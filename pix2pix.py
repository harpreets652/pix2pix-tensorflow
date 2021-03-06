from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np
import argparse
import os
import json
import random
import math
import time

import data_util as du
import model_util as mu

parser = argparse.ArgumentParser()
parser.add_argument("--input_dir", help="path to folder containing images")
parser.add_argument("--mode", required=True, choices=["train", "test", "export"])
parser.add_argument("--output_dir", required=True, help="where to put output files")
parser.add_argument("--seed", type=int)
parser.add_argument("--checkpoint", default=None,
                    help="directory with checkpoint to resume training from or use for testing")

parser.add_argument("--max_steps", type=int, help="number of training steps (0 to disable)")
parser.add_argument("--max_epochs", type=int, help="number of training epochs")
parser.add_argument("--summary_freq", type=int, default=100, help="update summaries every summary_freq steps")
parser.add_argument("--progress_freq", type=int, default=50, help="display progress every progress_freq steps")
parser.add_argument("--trace_freq", type=int, default=0, help="trace execution every trace_freq steps")
parser.add_argument("--display_freq", type=int, default=0,
                    help="write current training images every display_freq steps")
parser.add_argument("--save_freq", type=int, default=500, help="save model every save_freq steps, 0 to disable")

parser.add_argument("--separable_conv", action="store_true", help="use separable convolutions in the generator")
parser.add_argument("--aspect_ratio", type=float, default=1.0, help="aspect ratio of output images (width/height)")
parser.add_argument("--lab_colorization", action="store_true",
                    help="split input image into brightness (A) and color (B)")
parser.add_argument("--batch_size", type=int, default=1, help="number of images in batch")
parser.add_argument("--which_direction", type=str, default="AtoB", choices=["AtoB", "BtoA"])
parser.add_argument("--ngf", type=int, default=64, help="number of generator filters in first conv layer")
parser.add_argument("--ndf", type=int, default=64, help="number of discriminator filters in first conv layer")
parser.add_argument("--scale_size", type=int, default=286, help="scale images to this size before cropping to 256x256")
parser.add_argument("--flip", dest="flip", action="store_true", help="flip images horizontally")
parser.add_argument("--no_flip", dest="flip", action="store_false", help="don't flip images horizontally")
parser.set_defaults(flip=True)
parser.add_argument("--lr", type=float, default=0.0002, help="initial learning rate for adam")
parser.add_argument("--beta1", type=float, default=0.5, help="momentum term of adam")
parser.add_argument("--l1_weight", type=float, default=100.0, help="weight on L1 term for generator gradient")
parser.add_argument("--gan_weight", type=float, default=1.0, help="weight on GAN term for generator gradient")

# export options
parser.add_argument("--output_filetype", default="png", choices=["png", "jpeg"])
arguments = parser.parse_args()


def pre_process(image):
    with tf.name_scope("preprocess"):
        # [0, 1] => [-1, 1]
        return image * 2 - 1


def de_process(image):
    with tf.name_scope("deprocess"):
        # [-1, 1] => [0, 1]
        return (image + 1) / 2


def save_images(fetches, step=None):
    image_dir = os.path.join(arguments.output_dir, "images")
    if not os.path.exists(image_dir):
        os.makedirs(image_dir)

    filesets = []
    # note~ I changed this...this is broken and so is display_freq
    for i, in_path in enumerate(fetches["paths"]):
        name, _ = os.path.splitext(os.path.basename(in_path.decode("utf8")))
        fileset = {"name": name, "step": step}
        for kind in ["inputs", "outputs", "targets"]:
            filename = name + "-" + kind + ".png"
            if step is not None:
                filename = "%08d-%s" % (step, filename)
            fileset[kind] = filename
            out_path = os.path.join(image_dir, filename)
            contents = fetches[kind][i]
            with open(out_path, "wb") as f:
                f.write(contents)
        filesets.append(fileset)
    return filesets


def save_image(input_file_name, fetches):
    image_dir = os.path.join(arguments.output_dir, "images")
    if not os.path.exists(image_dir):
        os.makedirs(image_dir)

    name, _ = os.path.splitext(os.path.basename(input_file_name))

    output_fileset = {"name": name, "step": None}

    filename = name + "-" + "outputs" + ".png"
    output_fileset["outputs"] = filename

    out_path = os.path.join(image_dir, filename)
    contents = fetches["outputs"][0]
    with open(out_path, "wb") as f:
        f.write(contents)

    output_fileset["inputs"] = input_file_name

    return output_fileset


def append_index(fileset, step=False):
    index_path = os.path.join(arguments.output_dir, "index.html")
    if os.path.exists(index_path):
        index = open(index_path, "a")
    else:
        index = open(index_path, "w")
        index.write("<html><body><table><tr>")
        if step:
            index.write("<th>step</th>")
        index.write("<th>name</th><th>output</th><th>input</th></tr>")

    index.write("<tr>")

    if step:
        index.write("<td>%d</td>" % fileset["step"])

    index.write("<td>%s</td>" % fileset["name"])

    index.write("<td><img src='images/%s'></td>" % fileset["outputs"])
    index.write("<td><img src='../%s'></td>" % fileset["inputs"])

    index.write("</tr>")
    return index_path


def main():
    if arguments.seed is None:
        arguments.seed = random.randint(0, 2 ** 31 - 1)

    tf.set_random_seed(arguments.seed)
    np.random.seed(arguments.seed)
    random.seed(arguments.seed)

    if not os.path.exists(arguments.output_dir):
        os.makedirs(arguments.output_dir)

    if arguments.mode == "test" or arguments.mode == "export":
        if arguments.checkpoint is None:
            raise Exception("checkpoint required for test mode")

        # load some options from the checkpoint
        options = {"which_direction", "ngf", "ndf", "lab_colorization"}
        with open(os.path.join(arguments.checkpoint, "options.json")) as f:
            for key, val in json.loads(f.read()).items():
                if key in options:
                    print("loaded", key, "=", val)
                    setattr(arguments, key, val)
        # disable these features in test mode
        arguments.scale_size = mu.CROP_SIZE
        arguments.flip = False

    for k, v in arguments._get_kwargs():
        print(k, "=", v)

    with open(os.path.join(arguments.output_dir, "options.json"), "w") as f:
        f.write(json.dumps(vars(arguments), sort_keys=True, indent=4))

    if arguments.mode == "export":
        # export the generator to arguments meta graph that can be imported later for standalone generation
        if arguments.lab_colorization:
            raise Exception("export not supported for lab_colorization")

        input = tf.placeholder(tf.string, shape=[1])
        input_data = tf.decode_base64(input[0])
        input_image = tf.image.decode_png(input_data)

        # remove alpha channel if present
        input_image = tf.cond(tf.equal(tf.shape(input_image)[2], 4), lambda: input_image[:, :, :3], lambda: input_image)
        # convert grayscale to RGB
        input_image = tf.cond(tf.equal(tf.shape(input_image)[2], 1), lambda: tf.image.grayscale_to_rgb(input_image),
                              lambda: input_image)

        input_image = tf.image.convert_image_dtype(input_image, dtype=tf.float32)
        input_image.set_shape([mu.CROP_SIZE, mu.CROP_SIZE, 3])
        batch_input = tf.expand_dims(input_image, axis=0)

        with tf.variable_scope("generator"):
            batch_output = de_process(mu.create_generator(arguments, pre_process(batch_input), 3))

        output_image = tf.image.convert_image_dtype(batch_output, dtype=tf.uint8)[0]
        if arguments.output_filetype == "png":
            output_data = tf.image.encode_png(output_image)
        elif arguments.output_filetype == "jpeg":
            output_data = tf.image.encode_jpeg(output_image, quality=80)
        else:
            raise Exception("invalid filetype")

        output = tf.convert_to_tensor([tf.encode_base64(output_data)])

        key = tf.placeholder(tf.string, shape=[1])
        inputs = {
            "key": key.name,
            "input": input.name
        }
        tf.add_to_collection("inputs", json.dumps(inputs))

        outputs = {
            "key": tf.identity(key).name,
            "output": output.name,
        }
        tf.add_to_collection("outputs", json.dumps(outputs))

        init_op = tf.global_variables_initializer()
        restore_saver = tf.train.Saver()
        export_saver = tf.train.Saver()

        with tf.Session() as sess:
            sess.run(init_op)
            print("loading model from checkpoint")
            checkpoint = tf.train.latest_checkpoint(arguments.checkpoint)
            restore_saver.restore(sess, checkpoint)
            print("exporting model")
            export_saver.export_meta_graph(filename=os.path.join(arguments.output_dir, "export.meta"))
            export_saver.save(sess, os.path.join(arguments.output_dir, "export"), write_meta_graph=False)

        return

    # load training data
    # inputs and targets are [batch_size, height, width, channels]
    source_placeholder = tf.placeholder(tf.float32, (None, 256, 256, 3), "x_source")
    target_placeholder = tf.placeholder(tf.float32, (None, 256, 256, 3), "y_target")

    model = mu.create_model(arguments, source_placeholder, target_placeholder)

    outputs = de_process(model.outputs)

    def convert(image):
        if arguments.aspect_ratio != 1.0:
            # upscale to correct aspect ratio
            size = [mu.CROP_SIZE, int(round(mu.CROP_SIZE * arguments.aspect_ratio))]
            image = tf.image.resize_images(image, size=size, method=tf.image.ResizeMethod.BICUBIC)

        return tf.image.convert_image_dtype(image, dtype=tf.uint8, saturate=True)

    # reverse any processing on images so they can be written to disk or displayed to user
    with tf.name_scope("convert_outputs"):
        converted_outputs = convert(outputs)

    with tf.name_scope("encode_images"):
        display_fetches = {
            "outputs": tf.map_fn(tf.image.encode_png, converted_outputs, dtype=tf.string, name="output_pngs"),
        }

    # summaries
    with tf.name_scope("outputs_summary"):
        tf.summary.image("outputs", converted_outputs)

    with tf.name_scope("predict_real_summary"):
        tf.summary.image("predict_real", tf.image.convert_image_dtype(model.predict_real, dtype=tf.uint8))

    with tf.name_scope("predict_fake_summary"):
        tf.summary.image("predict_fake", tf.image.convert_image_dtype(model.predict_fake, dtype=tf.uint8))

    tf.summary.scalar("discriminator_loss", model.discrim_loss)
    tf.summary.scalar("generator_loss_GAN", model.gen_loss_GAN)
    tf.summary.scalar("generator_loss_L1", model.gen_loss_L1)

    for var in tf.trainable_variables():
        tf.summary.histogram(var.op.name + "/values", var)

    for grad, var in model.discrim_grads_and_vars + model.gen_grads_and_vars:
        tf.summary.histogram(var.op.name + "/gradients", grad)

    with tf.name_scope("parameter_count"):
        parameter_count = tf.reduce_sum([tf.reduce_prod(tf.shape(v)) for v in tf.trainable_variables()])

    saver = tf.train.Saver(max_to_keep=1)

    log_dir = arguments.output_dir if (arguments.trace_freq > 0 or arguments.summary_freq > 0) else None
    # todo: Supervisor is deprecated, use MonitoredTrainingSession
    sv = tf.train.Supervisor(logdir=log_dir, save_summaries_secs=0, saver=None)

    # training loop
    with sv.managed_session() as sess:

        images_filename_list = du.get_data_files_list(arguments.input_dir, "jpg")
        steps_per_epoch = int(math.ceil(len(images_filename_list) / arguments.batch_size))

        print("parameter_count =", sess.run(parameter_count))
        print(f"Data count = {len(images_filename_list)}")

        if arguments.checkpoint is not None:
            print("loading model from checkpoint")
            checkpoint = tf.train.latest_checkpoint(arguments.checkpoint)
            saver.restore(sess, checkpoint)

        max_steps = 2 ** 32
        if arguments.max_epochs is not None:
            max_steps = steps_per_epoch * arguments.max_epochs
        if arguments.max_steps is not None:
            max_steps = arguments.max_steps

        if arguments.mode == "test":
            # testing
            start = time.time()
            for test_file in images_filename_list:
                x_source, y_target = du.read_single_data_file(test_file, arguments)

                results = sess.run(display_fetches,
                                   feed_dict={source_placeholder: np.expand_dims(x_source, 0),
                                              target_placeholder: np.expand_dims(y_target, 0)})

                fileset = save_image(test_file, results)
                print("evaluated image", fileset["name"])
                index_path = append_index(fileset)

                print("wrote index at", index_path)

            print(f"time: {(time.time() - start)}")
        else:
            # training
            start = time.time()

            for step in range(max_steps):
                def should(freq):
                    return freq > 0 and ((step + 1) % freq == 0 or step == max_steps - 1)

                options = None
                run_metadata = None
                if should(arguments.trace_freq):
                    options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                    run_metadata = tf.RunMetadata()

                fetches = {
                    "train": model.train,
                    "global_step": sv.global_step,
                }

                if should(arguments.progress_freq):
                    fetches["discrim_loss"] = model.discrim_loss
                    fetches["gen_loss_GAN"] = model.gen_loss_GAN
                    fetches["gen_loss_L1"] = model.gen_loss_L1

                if should(arguments.summary_freq):
                    fetches["summary"] = sv.summary_op

                if should(arguments.display_freq):
                    fetches["display"] = display_fetches

                x_source, y_target = du.generate_batch(images_filename_list, arguments)

                results = sess.run(fetches, options=options, run_metadata=run_metadata,
                                   feed_dict={source_placeholder: x_source, target_placeholder: y_target})

                if should(arguments.summary_freq):
                    print("recording summary")
                    sv.summary_writer.add_summary(results["summary"], results["global_step"])

                if should(arguments.display_freq):
                    # todo: broken, need to fix
                    print("saving display images")
                    filesets = save_images(results["display"], step=results["global_step"])
                    append_index(filesets, step=True)

                if should(arguments.trace_freq):
                    print("recording trace")
                    sv.summary_writer.add_run_metadata(run_metadata, "step_%d" % results["global_step"])

                if should(arguments.progress_freq):
                    # global_step will have the correct step count if we resume from arguments checkpoint
                    train_epoch = math.ceil(results["global_step"] / steps_per_epoch)
                    train_step = (results["global_step"] - 1) % steps_per_epoch + 1
                    rate = (step + 1) * arguments.batch_size / (time.time() - start)
                    remaining = (max_steps - step) * arguments.batch_size / rate
                    print("progress  epoch %d  step %d  image/sec %0.1f  remaining %dm" % (
                        train_epoch, train_step, rate, remaining / 60))
                    print("discrim_loss", results["discrim_loss"])
                    print("gen_loss_GAN", results["gen_loss_GAN"])
                    print("gen_loss_L1", results["gen_loss_L1"])

                if should(arguments.save_freq):
                    print("saving model")
                    saver.save(sess, os.path.join(arguments.output_dir, "model"), global_step=sv.global_step)

                if sv.should_stop():
                    break


main()
