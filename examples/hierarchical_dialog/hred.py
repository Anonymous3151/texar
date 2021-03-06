#
""" Example for HRED structure.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# pylint: disable=invalid-name, no-name-in-module

import os
import numpy as np
import tensorflow as tf
import texar as tx

from texar.modules.encoders.hierarchical_encoders import HierarchicalRNNEncoder
from texar.modules.decoders.beam_search_decode import beam_search_decode

from tensorflow.contrib.seq2seq import tile_batch

from argparse import ArgumentParser

from config_data import data_root, max_utterance_cnt, data_hparams

import importlib

from nltk.translate.bleu_score import sentence_bleu
from nltk.translate.bleu_score import SmoothingFunction

flags = tf.flags
flags.DEFINE_string('config_model', 'config_model_biminor', 'The model config')
FLAGS = flags.FLAGS
config_model = importlib.import_module(FLAGS.config_model)

encoder_hparams = config_model.encoder_hparams
decoder_hparams = config_model.decoder_hparams
opt_hparams = config_model.opt_hparams

def main():
    # model part: data
    train_data = tx.data.MultiAlignedData(data_hparams['train'])
    val_data = tx.data.MultiAlignedData(data_hparams['val'])
    test_data = tx.data.MultiAlignedData(data_hparams['test'])
    iterator = tx.data.TrainTestDataIterator(train=train_data,
                                             val=val_data,
                                             test=test_data)
    data_batch = iterator.get_next()
    spk_src = tf.stack([data_batch['spk_{}'.format(i)]
                        for i in range(max_utterance_cnt)], 1)
    spk_tgt = data_batch['spk_tgt']

    # model part: hred
    def add_source_speaker_token(x):
        return tf.concat([x, tf.reshape(spk_src, (-1, 1))], 1)

    def add_target_speaker_token(x):
        return (x, ) + (tf.reshape(spk_tgt, (-1, 1)), )

    embedder = tx.modules.WordEmbedder(
        init_value=train_data.embedding_init_value(0).word_vecs)
    encoder = HierarchicalRNNEncoder(hparams=encoder_hparams)

    decoder = tx.modules.BasicRNNDecoder(
        hparams=decoder_hparams, vocab_size=train_data.vocab(0).size)

    connector = tx.modules.connectors.MLPTransformConnector(
        decoder.cell.state_size)

    # build tf graph

    context_embed = embedder(data_batch['source_text_ids'])
    ecdr_states = encoder(
        context_embed,
        medium=['flatten', add_source_speaker_token],
        sequence_length_minor=data_batch['source_length'],
        sequence_length_major=data_batch['source_utterance_cnt'])
    ecdr_states = ecdr_states[1]

    ecdr_states = add_target_speaker_token(ecdr_states)
    dcdr_states = connector(ecdr_states)

    # train branch

    target_embed = embedder(data_batch['target_text_ids'])
    outputs, _, lengths = decoder(
        initial_state=dcdr_states,
        inputs=target_embed,
        sequence_length=data_batch['target_length'] - 1)

    mle_loss = tx.losses.sequence_sparse_softmax_cross_entropy(
        labels=data_batch['target_text_ids'][:, 1:],
        logits=outputs.logits,
        sequence_length=lengths,
        sum_over_timesteps=False,
        average_across_timesteps=True)

    global_step = tf.Variable(0, name='global_step', trainable=True)
    train_op = tx.core.get_train_op(
        mle_loss, global_step=global_step, hparams=opt_hparams)

    perplexity = tf.exp(mle_loss)

    # beam search
    target_bos_token_id = train_data.vocab(0).bos_token_id
    target_eos_token_id = train_data.vocab(0).eos_token_id
    start_tokens = \
        tf.ones_like(data_batch['target_length']) * target_bos_token_id

    beam_search_samples, beam_states, _ = beam_search_decode(
        decoder,
        initial_state=dcdr_states,
        start_tokens=start_tokens,
        end_token=target_eos_token_id,
        embedding=embedder,
        beam_width=config_model.beam_width,
        max_decoding_length=50)

    beam_lengths = beam_states.lengths
    beam_sample_text = train_data.vocab(0).map_ids_to_tokens(
        beam_search_samples.predicted_ids)

    def _train_epochs(sess, epoch, display=1000):
        iterator.switch_to_train_data(sess)

        for i in range(3000):
            try:
                feed = {tx.global_mode(): tf.estimator.ModeKeys.TRAIN}
                step, loss, _ = sess.run(
                    [global_step, mle_loss, train_op], feed_dict=feed)

                if step % display == 0:
                    print('step {} at epoch {}: loss={}'.format(
                        step, epoch, loss))

            except tf.errors.OutOfRangeError:
                break

        print('epoch {} train fin: loss={}'.format(epoch, loss))

    def _test_epochs_ppl(sess, epoch):
        iterator.switch_to_test_data(sess)

        pples = []
        while True:
            try:
                feed = {tx.global_mode(): tf.estimator.ModeKeys.EVAL}
                ppl = sess.run(perplexity, feed_dict=feed)
                pples.append(loss)

            except tf.errors.OutOfRangeError:
                avg_ppl = np.mean(pples)
                print('epoch {} perplexity={}'.format(epoch, avg_ppl))
                break

    def _test_epochs_bleu(sess, epoch):
        iterator.switch_to_test_data(sess)

        bleu_prec = [[] for i in range(1, 5)]
        bleu_recall = [[] for i in range(1, 5)]

        def bleus(ref, sample):
            res = []
            for weight in [[1, 0, 0, 0],
                           [1, 0, 0, 0],
                           [0, 1, 0, 0],
                           [0, 0, 1, 0],
                           [0, 0, 0, 1]]:
                res.append(sentence_bleu([ref], sample,
                    smoothing_function=SmoothingFunction().method7,
                    weights=weight))
            return res

        while True:
            try:
                feed = {tx.global_mode(): tf.estimator.ModeKeys.EVAL}

                beam_samples, beam_length, references, refs_cnt = \
                    sess.run([beam_sample_text, beam_lengths,
                        data_batch['refs_text'][:, :, 1:],
                        data_batch['refs_utterance_cnt']],
                    feed_dict=feed)

                beam_samples = np.transpose(beam_samples, (0, 2, 1))
                beam_samples = [[sample[:l] for sample, l in zip(beam, lens)]
                    for beam, lens in zip(beam_samples.tolist(), beam_length)]
                references = [[ref[:ref.index(b'<EOS>')] for ref in refs[:cnt]]
                    for refs, cnt in zip(references.tolist(), refs_cnt)]

                for beam, refs in zip(beam_samples, references):
                    bleu_scores = np.array([[bleus(ref, sample)
                        for i, ref in enumerate(refs)]
                        for j, sample in enumerate(beam)])
                    bleu_scores = np.transpose(bleu_scores, (2, 0, 1))

                    for i in range(1, 5):
                        bleu_i = bleu_scores[i]
                        bleu_i_precision = bleu_i.max(axis=1).mean()
                        bleu_i_recall = bleu_i.max(axis=0).mean()

                        bleu_prec[i-1].append(bleu_i_precision)
                        bleu_recall[i-1].append(bleu_i_recall)


            except tf.errors.OutOfRangeError:
                break

        bleu_prec = [np.mean(x) for x in bleu_prec]
        bleu_recall = [np.mean(x) for x in bleu_recall]

        print('epoch {}:'.format(epoch))
        for i in range(1, 5):
            print(' -- bleu-{} prec={}, recall={}'.format(
                i, bleu_prec[i-1], bleu_recall[i-1]))

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())
        sess.run(tf.tables_initializer())

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)

        for epoch in range(10):
            _train_epochs(sess, epoch)
            _test_epochs_ppl(sess, epoch)

        _test_epochs_bleu(sess, epoch)

if __name__ == "__main__":
    main()
