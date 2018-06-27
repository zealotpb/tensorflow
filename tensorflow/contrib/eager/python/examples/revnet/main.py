# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Eager execution workflow with RevNet train on CIFAR-10."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys

from absl import flags
import tensorflow as tf
from tqdm import tqdm
from tensorflow.contrib.eager.python.examples.revnet import cifar_input
from tensorflow.contrib.eager.python.examples.revnet import config as config_
from tensorflow.contrib.eager.python.examples.revnet import revnet
tfe = tf.contrib.eager


def main(_):
  """Eager execution workflow with RevNet trained on CIFAR-10."""
  if FLAGS.data_dir is None:
    raise ValueError("No supplied data directory")

  if not os.path.exists(FLAGS.data_dir):
    raise ValueError("Data directory {} does not exist".format(FLAGS.data_dir))

  tf.enable_eager_execution()
  config = config_.get_hparams_cifar_38()

  if FLAGS.validate:
    # 40k Training set
    ds_train = cifar_input.get_ds_from_tfrecords(
        data_dir=FLAGS.data_dir,
        split="train",
        data_aug=True,
        batch_size=config.batch_size,
        epochs=config.epochs,
        shuffle=config.shuffle,
        data_format=config.data_format,
        dtype=config.dtype,
        prefetch=config.batch_size)
    # 10k Training set
    ds_validation = cifar_input.get_ds_from_tfrecords(
        data_dir=FLAGS.data_dir,
        split="validation",
        data_aug=False,
        batch_size=config.eval_batch_size,
        epochs=1,
        shuffle=False,
        data_format=config.data_format,
        dtype=config.dtype,
        prefetch=config.eval_batch_size)
  else:
    # 50k Training set
    ds_train = cifar_input.get_ds_from_tfrecords(
        data_dir=FLAGS.data_dir,
        split="train_all",
        data_aug=True,
        batch_size=config.batch_size,
        epochs=config.epochs,
        shuffle=config.shuffle,
        data_format=config.data_format,
        dtype=config.dtype,
        prefetch=config.batch_size)

  # Always compute loss and accuracy on whole training and test set
  ds_train_one_shot = cifar_input.get_ds_from_tfrecords(
      data_dir=FLAGS.data_dir,
      split="train_all",
      data_aug=False,
      batch_size=config.eval_batch_size,
      epochs=1,
      shuffle=False,
      data_format=config.data_format,
      dtype=config.dtype,
      prefetch=config.eval_batch_size)

  ds_test = cifar_input.get_ds_from_tfrecords(
      data_dir=FLAGS.data_dir,
      split="test",
      data_aug=False,
      batch_size=config.eval_batch_size,
      epochs=1,
      shuffle=False,
      data_format=config.data_format,
      dtype=config.dtype,
      prefetch=config.eval_batch_size)

  model = revnet.RevNet(config=config)
  global_step = tfe.Variable(1, trainable=False)
  learning_rate = tf.train.piecewise_constant(
      global_step, config.lr_decay_steps, config.lr_list)
  optimizer = tf.train.MomentumOptimizer(
      learning_rate, momentum=config.momentum)
  checkpointer = tf.train.Checkpoint(
      optimizer=optimizer, model=model, optimizer_step=global_step)

  if FLAGS.train_dir:
    summary_writer = tf.contrib.summary.create_file_writer(FLAGS.train_dir)
    if FLAGS.restore:
      latest_path = tf.train.latest_checkpoint(FLAGS.train_dir)
      checkpointer.restore(latest_path)
      print("Restored latest checkpoint at path:\"{}\" "
            "with global_step: {}".format(latest_path, global_step.numpy()))
      sys.stdout.flush()

  warmup(model, config)

  for x, y in ds_train:
    loss = train_one_iter(model, x, y, optimizer, global_step=global_step)

    if global_step.numpy() % config.log_every == 0:
      it_train = ds_train_one_shot.make_one_shot_iterator()
      acc_train, loss_train = evaluate(model, it_train)
      it_test = ds_test.make_one_shot_iterator()
      acc_test, loss_test = evaluate(model, it_test)
      if FLAGS.validate:
        it_validation = ds_validation.make_one_shot_iterator()
        acc_validation, loss_validation = evaluate(model, it_validation)
        print("Iter {}, "
              "training set accuracy {:.4f}, loss {:.4f}; "
              "validation set accuracy {:.4f}, loss {:4.f}"
              "test accuracy {:.4f}, loss {:.4f}".format(
                  global_step.numpy(), acc_train, loss_train, acc_validation,
                  loss_validation, acc_test, loss_test))
      else:
        print("Iter {}, "
              "training set accuracy {:.4f}, loss {:.4f}; "
              "test accuracy {:.4f}, loss {:.4f}".format(
                  global_step.numpy(), acc_train, loss_train, acc_test,
                  loss_test))
      sys.stdout.flush()

      if FLAGS.train_dir:
        with summary_writer.as_default():
          with tf.contrib.summary.always_record_summaries():
            tf.contrib.summary.scalar("Training loss", loss)
            tf.contrib.summary.scalar("Test accuracy", acc_test)
            if FLAGS.validate:
              tf.contrib.summary.scalar("Validation accuracy", acc_validation)

    if global_step.numpy() % config.save_every == 0 and FLAGS.train_dir:
      saved_path = checkpointer.save(
          file_prefix=os.path.join(FLAGS.train_dir, "ckpt"))
      print("Saved checkpoint at path: \"{}\" "
            "with global_step: {}".format(saved_path, global_step.numpy()))
      sys.stdout.flush()


def warmup(model, config, steps=1):
  mock_input = tf.random_normal((config.batch_size,) + config.input_shape)
  for _ in range(steps):
    model(mock_input, training=False)


def train_one_iter(model,
                   inputs,
                   labels,
                   optimizer,
                   global_step=None,
                   verbose=False):
  """Train for one iteration."""
  if FLAGS.manual_grad:
    if verbose:
      print("Using manual gradients")
    grads, vars_, loss = model.compute_gradients(inputs, labels)
    optimizer.apply_gradients(zip(grads, vars_), global_step=global_step)
  else:  # For correctness validation
    if verbose:
      print("Not using manual gradients")
    with tf.GradientTape() as tape:
      logits, _ = model(inputs, training=True)
      loss = model.compute_loss(logits=logits, labels=labels)
    grads = tape.gradient(loss, model.trainable_variables)
    optimizer.apply_gradients(
        zip(grads, model.trainable_variables), global_step=global_step)

  return loss.numpy()


def evaluate(model, iterator):
  """Compute accuracy with the given dataset iterator."""
  mean_loss = tfe.metrics.Mean()
  accuracy = tfe.metrics.Accuracy()
  for x, y in tqdm(iterator):
    logits, _ = model(x, training=False)
    loss = model.compute_loss(logits=logits, labels=y)
    accuracy(
        labels=tf.cast(y, tf.int64),
        predictions=tf.argmax(logits, axis=1, output_type=tf.int64))
    mean_loss(loss)

  return accuracy.result().numpy(), mean_loss.result().numpy()


if __name__ == "__main__":
  flags.DEFINE_string(
      "train_dir",
      default=None,
      help="[Optional] Directory to store the training information")
  flags.DEFINE_string(
      "data_dir", default=None, help="Directory to load tfrecords")
  flags.DEFINE_boolean(
      "restore",
      default=False,
      help="[Optional] Restore the latest checkpoint from `train_dir` if True")
  flags.DEFINE_boolean(
      "validate",
      default=False,
      help="[Optional] Use the validation set or not for hyperparameter search")
  flags.DEFINE_boolean(
      "manual_grad",
      default=False,
      help="[Optional] Use manual gradient graph to save memory")
  FLAGS = flags.FLAGS
  tf.app.run(main)
