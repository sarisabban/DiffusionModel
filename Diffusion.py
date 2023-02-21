# -*- coding: utf-8 -*-
"""Diffusion.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/18YtLGCqF6hJRcbZnENPTJ7IOCMlpkjVF
"""

'''
References:
https://medium.com/@vedantjumle/image-generation-with-diffusion-models-using-keras-and-tensorflow-9f60aae72ac
https://arxiv.org/abs/2006.11239
https://arxiv.org/abs/2010.02502
'''

!pip install tensorflow
!pip install tensorflow_datasets
!pip install tensorflow_addons
!pip install tensorflow_federated
!pip install einops
!rm -r sample_data/

import os
import math
import inspect
import numpy as np
import matplotlib.pyplot as plt
import tensorflow as tf
import tensorflow.keras.layers as nn
import tensorflow_addons as tfa
import tensorflow_datasets as tfds
import tensorflow_federated as tff
from tensorflow import keras, einsum
from tensorflow.keras import Model, Sequential
from tensorflow.keras.layers import Layer
from einops import rearrange
from einops.layers.tensorflow import Rearrange
from functools import partial

batches   = 64
timesteps = 200
epochs    = 1

##### DATASET MNIST #####
def preprocess(x, y):
    return tf.image.resize(tf.cast(x, tf.float32) / 127.5 - 1, (32, 32))
def get_datasets():
    train_ds = tfds.load('mnist', as_supervised=True, split='train')
    train_ds = train_ds.map(preprocess, tf.data.AUTOTUNE)
    train_ds = train_ds.shuffle(5000).batch(batches).prefetch(tf.data.AUTOTUNE)
    return tfds.as_numpy(train_ds)
dataset = get_datasets() # shape = (60000, 32, 32, 1)
##### DATASET MNIST #####

X = np.random.randint(low=1, high=5, size=(1000, 16, 16, 16))
X = tf.data.Dataset.from_tensor_slices(X)
X = X.shuffle(1).batch(batches)
X = tfds.as_numpy(X)
#dataset = X
example_shape = next(x.shape for x in iter(dataset))
shape = example_shape[1:3]
channels = example_shape[3]

def set_key(key):
    ''' Random seed control '''
    np.random.seed(key)

def forward_noise(key, x_0, t):
    ''' Forward noise function '''
    set_key(key)
    b = np.linspace(0.0001, 0.02, timesteps)
    a = 1 - b
    a_ = np.cumprod(a, 0)
    a_ = np.concatenate((np.array([1.]), a_[:-1]), axis=0)
    sqrt_a_ = np.sqrt(a_)
    sqrt_1_a_ = np.sqrt(1 - a_)
    noise = np.random.normal(size=x_0.shape)
    reshaped_sqrt_a_t = np.reshape(np.take(sqrt_a_, t), (-1, 1, 1, 1))
    reshaped_sqrt_1_a_t = np.reshape(np.take(sqrt_1_a_, t), (-1, 1, 1, 1))
    noisy_data = reshaped_sqrt_a_t * x_0 + reshaped_sqrt_1_a_t * noise
    return noisy_data, noise

def generate_timestamp(key, num):
    ''' Generate the timesteps '''
    set_key(key)
    return np.int32(np.random.uniform(size=[num], low=0, high=timesteps))

def ddim(x_t, pred_noise, t, sigma_t):
    ''' Backward denoise function using denoising diffusion implicit model '''
    b = np.linspace(0.0001, 0.02, timesteps)
    a = 1 - b
    a_ = np.cumprod(a, 0)
    a_ = np.concatenate((np.array([1.]), a_[:-1]), axis=0)
    sqrt_a_ = np.sqrt(a_)
    sqrt_1_a_ = np.sqrt(1 - a_)
    a_t_bar = np.take(a_, t)
    a_t_minus_one = np.take(a, t-1)
    pred = (x_t - ((1 - a_t_bar) ** 0.5) * pred_noise)/ (a_t_bar ** 0.5)
    pred = (a_t_minus_one ** 0.5) * pred
    pred = pred + ((1 - a_t_minus_one - (sigma_t ** 2)) ** 0.5) * pred_noise
    eps_t = np.random.normal(size=x_t.shape)
    pred = pred + (sigma_t * eps_t)
    return pred

def loss_fn(real, generated):
    ''' The mean squared error loss function '''
    loss = tf.math.reduce_mean((real - generated) ** 2)
    return loss

"""# U-NET"""

def exists(x):
    return x is not None

def default(val, d):
    if exists(val):
        return val
    return d() if inspect.isfunction(d) else d

class SinusoidalPosEmb(Layer):
    def __init__(self, dim, max_positions=10000):
        super(SinusoidalPosEmb, self).__init__()
        self.dim = dim
        self.max_positions = max_positions
    def call(self, x, training=True):
        x = tf.cast(x, tf.float32)
        half_dim = self.dim // 2
        emb = math.log(self.max_positions) / (half_dim - 1)
        emb = tf.exp(tf.range(half_dim, dtype=tf.float32) * -emb)
        emb = x[:, None] * emb[None, :]
        emb = tf.concat([tf.sin(emb), tf.cos(emb)], axis=-1)
        return emb
        
class Identity(Layer):
    def __init__(self):
        super(Identity, self).__init__()
    def call(self, x, training=True):
        return tf.identity(x)

class Residual(Layer):
    def __init__(self, fn):
        super(Residual, self).__init__()
        self.fn = fn
    def call(self, x, training=True):
        return self.fn(x, training=training) + x

def Upsample(dim):
    return nn.Conv2DTranspose(filters=dim, kernel_size=4, strides=2, padding='SAME')

def Downsample(dim):
    return nn.Conv2D(filters=dim, kernel_size=4, strides=2, padding='SAME')

class LayerNorm(Layer):
    def __init__(self, dim, eps=1e-5, **kwargs):
        super(LayerNorm, self).__init__(**kwargs)
        self.eps = eps
        self.g = tf.Variable(tf.ones([1, 1, 1, dim]))
        self.b = tf.Variable(tf.zeros([1, 1, 1, dim]))
    def call(self, x, training=True):
        var = tf.math.reduce_variance(x, axis=-1, keepdims=True)
        mean = tf.reduce_mean(x, axis=-1, keepdims=True)
        x = (x - mean) / tf.sqrt((var + self.eps)) * self.g + self.b
        return x

class PreNorm(Layer):
    def __init__(self, dim, fn):
        super(PreNorm, self).__init__()
        self.fn = fn
        self.norm = LayerNorm(dim)
    def call(self, x, training=True):
        x = self.norm(x)
        return self.fn(x)

class SiLU(Layer):
    def __init__(self):
        super(SiLU, self).__init__()
    def call(self, x, training=True):
        return x * tf.nn.sigmoid(x)

def gelu(x, approximate=False):
    if approximate:
        coeff = tf.cast(0.044715, x.dtype)
        return 0.5 * x * (1.0 + tf.tanh(0.7978845608028654 * (x + coeff * tf.pow(x, 3))))
    else:
        return 0.5 * x * (1.0 + tf.math.erf(x / tf.cast(1.4142135623730951, x.dtype)))

class GELU(Layer):
    def __init__(self, approximate=False):
        super(GELU, self).__init__()
        self.approximate = approximate
    def call(self, x, training=True):
        return gelu(x, self.approximate)

class Block(Layer):
    def __init__(self, dim, groups=8):
        super(Block, self).__init__()
        self.proj = nn.Conv2D(dim, kernel_size=3, strides=1, padding='SAME')
        self.norm = tfa.layers.GroupNormalization(groups, epsilon=1e-05)
        self.act = SiLU()
    def call(self, x, gamma_beta=None, training=True):
        x = self.proj(x)
        x = self.norm(x, training=training)
        if exists(gamma_beta):
            gamma, beta = gamma_beta
            x = x * (gamma + 1) + beta
        x = self.act(x)
        return x

class ResnetBlock(Layer):
    def __init__(self, dim, dim_out, time_emb_dim=None, groups=8):
        super(ResnetBlock, self).__init__()
        self.mlp = Sequential([
            SiLU(),
            nn.Dense(units=dim_out * 2)
        ]) if exists(time_emb_dim) else None
        self.block1 = Block(dim_out, groups=groups)
        self.block2 = Block(dim_out, groups=groups)
        self.res_conv = nn.Conv2D(
            filters=dim_out, kernel_size=1, 
            strides=1) if dim != dim_out else Identity()
    def call(self, x, time_emb=None, training=True):
        gamma_beta = None
        if exists(self.mlp) and exists(time_emb):
            time_emb = self.mlp(time_emb)
            time_emb = rearrange(time_emb, 'b c -> b 1 1 c')
            gamma_beta = tf.split(time_emb, num_or_size_splits=2, axis=-1)
        h = self.block1(x, gamma_beta=gamma_beta, training=training)
        h = self.block2(h, training=training)
        return h + self.res_conv(x)

class LinearAttention(Layer):
    def __init__(self, dim, heads=4, dim_head=32):
        super(LinearAttention, self).__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads
        self.hidden_dim = dim_head * heads
        self.attend = nn.Softmax()
        self.to_qkv = nn.Conv2D(filters=self.hidden_dim * 3, kernel_size=1, strides=1, use_bias=False)
        self.to_out = Sequential([
            nn.Conv2D(filters=dim, kernel_size=1, strides=1),
            LayerNorm(dim)])
    def call(self, x, training=True):
        b, h, w, c = x.shape
        qkv = self.to_qkv(x)
        qkv = tf.split(qkv, num_or_size_splits=3, axis=-1)
        q, k, v = map(lambda t: rearrange(t, 'b x y (h c) -> b h c (x y)', h=self.heads), qkv)
        q = tf.nn.softmax(q, axis=-2)
        k = tf.nn.softmax(k, axis=-1)
        q = q * self.scale
        context = einsum('b h d n, b h e n -> b h d e', k, v)
        out = einsum('b h d e, b h d n -> b h e n', context, q)
        out = rearrange(out, 'b h c (x y) -> b x y (h c)', h=self.heads, x=h, y=w)
        out = self.to_out(out, training=training)
        return out

class Attention(Layer):
    def __init__(self, dim, heads=4, dim_head=32):
        super(Attention, self).__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads
        self.hidden_dim = dim_head * heads
        self.to_qkv = nn.Conv2D(filters=self.hidden_dim * 3, kernel_size=1, strides=1, use_bias=False)
        self.to_out = nn.Conv2D(filters=dim, kernel_size=1, strides=1)
    def call(self, x, training=True):
        b, h, w, c = x.shape
        qkv = self.to_qkv(x)
        qkv = tf.split(qkv, num_or_size_splits=3, axis=-1)
        q, k, v = map(lambda t: rearrange(t, 'b x y (h c) -> b h c (x y)', h=self.heads), qkv)
        q = q * self.scale
        sim = einsum('b h d i, b h d j -> b h i j', q, k)
        sim_max = tf.stop_gradient(tf.expand_dims(tf.argmax(sim, axis=-1), axis=-1))
        sim_max = tf.cast(sim_max, tf.float32)
        sim = sim - sim_max
        attn = tf.nn.softmax(sim, axis=-1)
        out = einsum('b h i j, b h d j -> b h i d', attn, v)
        out = rearrange(out, 'b h (x y) d -> b x y (h d)', x = h, y = w)
        out = self.to_out(out, training=training)
        return out

class Unet(Model):
    def __init__(self,
                 dim=64,
                 init_dim=None,
                 out_dim=None,
                 dim_mults=(1, 2, 4, 8),
                 channels=3,
                 resnet_block_groups=8,
                 learned_variance=False,
                 sinusoidal_cond_mlp=True):
        super(Unet, self).__init__()
        self.channels = channels
        init_dim = default(init_dim, dim // 3 * 2)
        self.init_conv = nn.Conv2D(
            filters=init_dim, kernel_size=7, strides=1, padding='SAME')
        dims = [init_dim, * map(lambda m: dim * m, dim_mults)]
        in_out = list(zip(dims[:-1], dims[1:]))
        block_klass = partial(ResnetBlock, groups = resnet_block_groups)
        time_dim = dim * 4
        self.sinusoidal_cond_mlp = sinusoidal_cond_mlp
        self.time_mlp = Sequential([
            SinusoidalPosEmb(dim),
            nn.Dense(units=time_dim),
            GELU(),
            nn.Dense(units=time_dim)
        ], name='time embeddings')
        self.downs = []
        self.ups = []
        num_resolutions = len(in_out)
        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (num_resolutions - 1)
            self.downs.append([
                block_klass(dim_in, dim_out, time_emb_dim=time_dim),
                block_klass(dim_out, dim_out, time_emb_dim=time_dim),
                Residual(PreNorm(dim_out, LinearAttention(dim_out))),
                Downsample(dim_out) if not is_last else Identity()])
        mid_dim = dims[-1]
        self.mid_block1 = block_klass(mid_dim, mid_dim, time_emb_dim=time_dim)
        self.mid_attn = Residual(PreNorm(mid_dim, Attention(mid_dim)))
        self.mid_block2 = block_klass(mid_dim, mid_dim, time_emb_dim=time_dim)
        for ind, (dim_in, dim_out) in enumerate(reversed(in_out[1:])):
            is_last = ind >= (num_resolutions - 1)
            self.ups.append([
                block_klass(dim_out * 2, dim_in, time_emb_dim=time_dim),
                block_klass(dim_in, dim_in, time_emb_dim=time_dim),
                Residual(PreNorm(dim_in, LinearAttention(dim_in))),
                Upsample(dim_in) if not is_last else Identity()])
        default_out_dim = channels * (1 if not learned_variance else 2)
        self.out_dim = default(out_dim, default_out_dim)
        self.final_conv = Sequential([
            block_klass(dim * 2, dim),
            nn.Conv2D(filters=self.out_dim, kernel_size=1, strides=1)
        ], name='output')
    def call(self, x, time=None, training=True, **kwargs):
        x = self.init_conv(x)
        t = self.time_mlp(time)
        h = []
        for block1, block2, attn, downsample in self.downs:
            x = block1(x, t)
            x = block2(x, t)
            x = attn(x)
            h.append(x)
            x = downsample(x)
        x = self.mid_block1(x, t)
        x = self.mid_attn(x)
        x = self.mid_block2(x, t)
        for block1, block2, attn, upsample in self.ups:
            x = tf.concat([x, h.pop()], axis=-1)
            x = block1(x, t)
            x = block2(x, t)
            x = attn(x)
            x = upsample(x)
        x = tf.concat([x, h.pop()], axis=-1)
        x = self.final_conv(x)
        return x

"""# Train / Generate"""

def train(epochs=1):
    unet = Unet(channels=channels)
    opt = keras.optimizers.Adam(learning_rate=1e-4)
    for e in range(1, epochs + 1):
        bar = tf.keras.utils.Progbar(len(dataset)-1)
        losses = []
        for i, batch in enumerate(iter(dataset)):
            rng, tsrng = np.random.randint(0, 1e5, size=(2,))
            timestep_values = generate_timestamp(tsrng, batch.shape[0])
            noised_data, noise = forward_noise(rng, batch, timestep_values)
            with tf.GradientTape() as tape:
                prediction = unet(noised_data, timestep_values)
                loss = loss_fn(noise, prediction)
            gradients = tape.gradient(loss, unet.trainable_variables)
            opt.apply_gradients(zip(gradients, unet.trainable_variables))
            losses.append(loss)
            bar.update(i, values=[('loss', loss)])
        avg = np.mean(losses)
        print(f'Average loss for epoch {e}/{epochs}: {avg}')
        tff.learning.models.save(unet, './weights') ########## not saving/loading properly

train(epochs)

def inference(inference_timesteps=10):
    ''' Generate data '''
    unet = tff.learning.models.load('./weights') ##########
    inference_range = range(0, timesteps, timesteps // inference_timesteps)
    x = np.random.normal(size=(1, shape[0], shape[1], channels))
    for i in reversed(range(inference_timesteps)):
        t = np.expand_dims(inference_range[i], 0)
        pred_noise = unet(x, t)
        x = ddim(x, pred_noise, t, 0)
        output = np.squeeze(x, 0)
        if channels == 1:
            output = np.squeeze(output,-1)
            plt.imshow(output, cmap='gray')
            plt.show()
        elif channels == 3:
            plt.imshow(output, interpolation='nearest')
            plt.show()

inference()