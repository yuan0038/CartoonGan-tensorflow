import os
import logging
import tensorflow as tf
from glob import glob
from tqdm import tqdm
from generator import Generator
from tensorflow.keras.applications import VGG19
import numpy as np
import matplotlib as mpl
mpl.use("agg")
import matplotlib.pyplot as plt
from datetime import datetime


class Trainer:
    def __init__(
        self,
        dataset_name,
        source_domain,
        target_domain,
        gan_type,
        conv_arch,
        num_generator_res_blocks,
        input_size,
        batch_size,
        sample_size,
        num_steps,
        reporting_steps,
        content_lambda,
        style_lambda,
        g_adv_lambda,
        d_adv_lambda,
        generator_lr,
        discriminator_lr,
        logger_name,
        data_dir,
        logdir,
        result_dir,
        pretrain_model_dir,
        model_dir,
        disable_sampling,
        ignore_vgg,
        pretrain_learning_rate,
        pretrain_epochs,
        pretrain_reporting_steps,
        pretrain_generator_name,
        generator_name,
        discriminator_name,
        **kwargs,
    ):
        self.ascii = os.name == "nt"
        self.dataset_name = dataset_name
        self.source_domain = source_domain
        self.target_domain = target_domain
        self.gan_type = gan_type
        self.conv_arch = conv_arch
        self.num_generator_res_blocks = num_generator_res_blocks
        self.input_size = input_size
        self.batch_size = batch_size
        self.sample_size = sample_size
        self.num_steps = num_steps
        self.reporting_steps = reporting_steps
        self.content_lambda = content_lambda
        self.style_lambda = style_lambda
        self.g_adv_lambda = g_adv_lambda
        self.d_adv_lambda = d_adv_lambda
        self.generator_lr = generator_lr
        self.discriminator_lr = discriminator_lr
        self.data_dir = data_dir
        self.logdir = logdir
        self.result_dir = result_dir
        self.pretrain_model_dir = pretrain_model_dir
        self.model_dir = model_dir
        self.disable_sampling = disable_sampling
        self.ignore_vgg = ignore_vgg
        self.pretrain_learning_rate = pretrain_learning_rate
        self.pretrain_epochs = pretrain_epochs
        self.pretrain_reporting_steps = pretrain_reporting_steps
        self.pretrain_generator_name = pretrain_generator_name
        self.generator_name = generator_name
        self.discriminator_name = discriminator_name

        self.logger = logging.getLogger(logger_name)

        if not self.ignore_vgg:
            logger.info("Setting up VGG19 for computing content loss...")
            input_shape = (self.input_size, self.input_size, 3)
            vgg19 = VGG19(weights="imagenet", include_top=False, input_shape=input_shape)
            self.vgg = tf.keras.Model(inputs=vgg19.input, outputs=vgg19.get_layer("block4_conv4").output)
        else:
            logger.info("VGG19 will not be used. Content loss will simply imply pixel-wise difference.")
            self.vgg = None

    def _save_generated_images(
        self, batch_x, image_name=None, num_images_per_row=8
    ):
        batch_size = batch_x.shape[0]
        num_rows = (
            batch_size // num_images_per_row if batch_size >= num_images_per_row else 1
        )
        fig_width = 12
        fig_height = 8
        fig = plt.figure(figsize=(fig_width, fig_height))
        for i in range(batch_size):
            fig.add_subplot(num_rows, num_images_per_row, i + 1)
            plt.imshow(batch_x[i])
            plt.axis("off")
        if image_name is not None:
            directory = self.result_dir
            if not os.path.exists(directory):
                os.makedirs(directory)
            plt.savefig(os.path.join(directory, image_name))
        plt.close(fig)

    def get_dataset(self, dataset_name, domain, _type, batch_size):
        files = glob(os.path.join(self.data_dir, dataset_name, f"{_type}{domain}", "*"))
        self.logger.info(
            f"Found {len(files)} domain{domain} images in {_type}{domain} folder."
        )

        ds = tf.data.Dataset.from_tensor_slices(files)

        def image_processing(filename):
            x = tf.io.read_file(filename)
            x = tf.image.decode_jpeg(x, channels=3)
            x = tf.image.random_crop(x, (self.input_size, self.input_size, 3))
            x = tf.image.resize_image_with_crop_or_pad(x, self.input_size, self.input_size)
            img = tf.cast(x, tf.float32) / 127.5 - 1
            return img

        return ds.map(image_processing).shuffle(6000).repeat().batch(batch_size)

    @tf.function
    def content_loss(self, input_images, generated_images):
        loss = tf.keras.losses.MeanAbsoluteError()
        if self.vgg:
            input_content = self.vgg(input_images)
            generated_content = self.vgg(generated_images)
        else:
            input_content = input_images
            generated_content = generated_images

        return self.content_lambda * loss(input_content, generated_content)

    @tf.function
    def pretrain_step(self, input_images, generator, optimizer):

        with tf.GradientTape() as tape:
            generated_images = generator(input_images)
            c_loss = self.content_loss(input_images, generated_images)

        gradients = tape.gradient(c_loss, generator.trainable_variables)
        optimizer.apply_gradients(zip(gradients, generator.trainable_variables))

    def pretrain_generator(self):
        self.logger.info(
            f"Building `{self.dataset_name}` dataset with domain `{self.source_domain}`..."
        )
        dataset = self.get_dataset(dataset_name=self.dataset_name,
                                   domain=self.source_domain,
                                   _type="train",
                                   batch_size=self.batch_size)
        self.logger.info(f"Initializing generator with "
                         f"batch_size: {self.batch_size}, input_size: {self.input_size}...")
        generator = Generator()
        generator(tf.keras.Input(
            shape=(self.input_size, self.input_size, 3),
            batch_size=self.batch_size))
        generator.summary()

        # TODO: checkpoint processing

        self.logger.info("Setting up optimizer to update generator's parameters...")
        optimizer = tf.keras.optimizers.Adam(learning_rate=self.pretrain_learning_rate)

        self.logger.info("Preparing seed images for monitoring model performance...")
        # TODO: use previous seed if available
        if not self.disable_sampling:
            self.logger.info(
                f"Sampling {self.sample_size} images for tracking generator's performance..."
            )
            real_batches = list()
            for image_batch in dataset.take(self.sample_size // self.batch_size):
                real_batches.append(image_batch)

            self._save_generated_images(
                (np.clip(np.concatenate(real_batches, axis=0), -1, 1) + 1) / 2,
                image_name="sample_images.png",
            )
        else:
            self.logger.info("Proceed training without sampling images...")

        self.logger.info("Starting training loop...")
        progress_bar = tqdm(list(range(self.pretrain_epochs)))
        for epoch in progress_bar:
            progress_bar.set_description(f"Epoch {epoch}")

            for step, image_batch in enumerate(dataset):
                self.pretrain_step(image_batch, generator, optimizer)

                if step % self.pretrain_reporting_steps == 0:

                    if not self.disable_sampling:
                        fake_batches = [generator(real_b) for real_b in real_batches]
                        self._save_generated_images(
                            (np.clip(np.concatenate(fake_batches, axis=0), -1, 1) + 1) / 2,
                            image_name=f"generated_images_at_step_{step}.png",
                        )

                    # TODO: save checkpoints
                    # self.logger.info(f"Saving checkpoints for step {step}...")
                    # g.save(sess, self.model_dir, self.pretrain_generator_name)
                    # generator.save_weights(os.path.join(self.pretrain_model_dir, "generator.h5"))
                    # self.logger.info(
                    #     "[Step {}] batch_loss: {:.3f}, {} elapsed".format(
                    #         step, batch_loss, datetime.utcnow() - start
                    #     )
                    # )

                    # TODO: tensorboard callback
                    # with open(os.path.join(self.result_dir, "batch_losses.tsv"), "a") as f:
                    #     f.write(f"{step}\t{batch_loss}\n")


def main(**kwargs):
    t = Trainer(**kwargs)

    mode = kwargs["mode"]
    if mode == "full":
        t.pretrain_generator()
        t.train_gan()
    elif mode == "pretrain":
        t.pretrain_generator()
    elif mode == "gan":
        t.train_gan()


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, default="full",
                        choices=["full", "pretrain", "gan"])
    parser.add_argument("--dataset_name", type=str, default="realworld2cartoon")
    parser.add_argument("--input_size", type=int, default=256)
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--sample_size", type=int, default=32)
    parser.add_argument("--source_domain", type=str, default="A")
    parser.add_argument("--target_domain", type=str, default="B")
    parser.add_argument("--gan_type", type=str, default="gan",
                        choices=["gan", "lsgan"])
    parser.add_argument("--conv_arch", type=str, default="conv_with_in",
                        choices=["conv_with_in", "coupled_conv",
                                 "coupled_conv_resblocks"])
    parser.add_argument("--num_generator_res_blocks", type=int, default=8)
    parser.add_argument("--num_steps", type=int, default=600_000)
    parser.add_argument("--reporting_steps", type=int, default=100)
    parser.add_argument("--content_lambda", type=float, default=10)
    parser.add_argument("--style_lambda", type=float, default=1.)
    parser.add_argument("--g_adv_lambda", type=float, default=1)
    parser.add_argument("--d_adv_lambda", type=float, default=1)
    parser.add_argument("--generator_lr", type=float, default=1e-4)
    parser.add_argument("--discriminator_lr", type=float, default=4e-4)
    parser.add_argument("--ignore_vgg", action="store_true")
    parser.add_argument("--pretrain_learning_rate", type=float, default=1e-5)
    parser.add_argument("--pretrain_epochs", type=int, default=10)
    parser.add_argument("--pretrain_reporting_steps", type=int, default=100)
    parser.add_argument("--data_dir", type=str, default="datasets")
    parser.add_argument("--logdir", type=str, default="runs")
    parser.add_argument("--result_dir", type=str, default="result")
    parser.add_argument("--pretrain_model_dir", type=str, default="ckpts")
    parser.add_argument("--model_dir", type=str, default="ckpts")
    parser.add_argument("--disable_sampling", type=bool, default=False)

    parser.add_argument(
        "--pretrain_generator_name", type=str, default="pretrain_generator"
    )
    parser.add_argument("--generator_name", type=str, default="generator")
    parser.add_argument("--discriminator_name", type=str, default="discriminator")
    parser.add_argument(
        "--logging_lvl",
        type=str,
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
    )
    parser.add_argument("--logger_out_file", type=str, default=None)
    parser.add_argument("--not_show_progress_bar", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--show_tf_cpp_log", action="store_true")

    args = parser.parse_args()

    if not args.show_tf_cpp_log:
        os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

    args.show_progress = not args.not_show_progress_bar
    log_lvl = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }
    args.logger_name = "Trainer"
    logger = logging.getLogger(args.logger_name)
    logger.propagate = False
    if args.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(log_lvl[args.logging_lvl])
    formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    stdhandler = logging.StreamHandler(sys.stdout)
    stdhandler.setFormatter(formatter)
    logger.addHandler(stdhandler)
    if args.logger_out_file is not None:
        fhandler = logging.StreamHandler(open(args.logger_out_file, "a"))
        fhandler.setFormatter(formatter)
        args.addHandler(fhandler)
    kwargs = vars(args)
    main(**kwargs)
