# -*- coding: utf-8 -*-

import os
from PIL import Image
import os.path
import time
import torch
import torchvision.datasets as dset
import torchvision.transforms as trn
import torch.utils.data as data
import numpy as np

from PIL import Image


# /////////////// Image-Level Distortion Methods ///////////////

import skimage as sk
from skimage.filters import gaussian
from io import BytesIO
from wand.image import Image as WandImage
from wand.api import library as wandlibrary
import wand.color as WandColor
import ctypes
from PIL import Image as PILImage
import cv2
from scipy.ndimage import zoom as scizoom
from scipy.ndimage.interpolation import map_coordinates
import warnings

warnings.simplefilter("ignore", UserWarning)


def disk(radius, alias_blur=0.1, dtype=np.float32):
    if radius <= 8:
        L = np.arange(-8, 8 + 1)
        ksize = (3, 3)
    else:
        L = np.arange(-radius, radius + 1)
        ksize = (5, 5)
    X, Y = np.meshgrid(L, L)
    aliased_disk = np.array((X ** 2 + Y ** 2) <= radius ** 2, dtype=dtype)
    aliased_disk /= np.sum(aliased_disk)

    # supersample disk to antialias
    return cv2.GaussianBlur(aliased_disk, ksize=ksize, sigmaX=alias_blur)


# Tell Python about the C method
wandlibrary.MagickMotionBlurImage.argtypes = (ctypes.c_void_p,  # wand
                                              ctypes.c_double,  # radius
                                              ctypes.c_double,  # sigma
                                              ctypes.c_double)  # angle


# Extend wand.image.Image class to include method signature
class MotionImage(WandImage):
    def motion_blur(self, radius=0.0, sigma=0.0, angle=0.0):
        wandlibrary.MagickMotionBlurImage(self.wand, radius, sigma, angle)


# modification of https://github.com/FLHerne/mapgen/blob/master/diamondsquare.py
def plasma_fractal(mapsize=32, wibbledecay=3):
    """
    Generate a heightmap using diamond-square algorithm.
    Return square 2d array, side length 'mapsize', of floats in range 0-255.
    'mapsize' must be a power of two.
    """
    assert (mapsize & (mapsize - 1) == 0)
    maparray = np.empty((mapsize, mapsize), dtype=np.float_)
    maparray[0, 0] = 0
    stepsize = mapsize
    wibble = 100

    def wibbledmean(array):
        return array / 4 + wibble * np.random.uniform(-wibble, wibble, array.shape)

    def fillsquares():
        """For each square of points stepsize apart,
           calculate middle value as mean of points + wibble"""
        cornerref = maparray[0:mapsize:stepsize, 0:mapsize:stepsize]
        squareaccum = cornerref + np.roll(cornerref, shift=-1, axis=0)
        squareaccum += np.roll(squareaccum, shift=-1, axis=1)
        maparray[stepsize // 2:mapsize:stepsize,
        stepsize // 2:mapsize:stepsize] = wibbledmean(squareaccum)

    def filldiamonds():
        """For each diamond of points stepsize apart,
           calculate middle value as mean of points + wibble"""
        mapsize = maparray.shape[0]
        drgrid = maparray[stepsize // 2:mapsize:stepsize, stepsize // 2:mapsize:stepsize]
        ulgrid = maparray[0:mapsize:stepsize, 0:mapsize:stepsize]
        ldrsum = drgrid + np.roll(drgrid, 1, axis=0)
        lulsum = ulgrid + np.roll(ulgrid, -1, axis=1)
        ltsum = ldrsum + lulsum
        maparray[0:mapsize:stepsize, stepsize // 2:mapsize:stepsize] = wibbledmean(ltsum)
        tdrsum = drgrid + np.roll(drgrid, 1, axis=1)
        tulsum = ulgrid + np.roll(ulgrid, -1, axis=0)
        ttsum = tdrsum + tulsum
        maparray[stepsize // 2:mapsize:stepsize, 0:mapsize:stepsize] = wibbledmean(ttsum)

    while stepsize >= 2:
        fillsquares()
        filldiamonds()
        stepsize //= 2
        wibble /= wibbledecay

    maparray -= maparray.min()
    return maparray / maparray.max()


def clipped_zoom(img, zoom_factor):
    h = img.shape[0]
    # ceil crop height(= crop width)
    ch = int(np.ceil(h / zoom_factor))

    top = (h - ch) // 2
    img = scizoom(img[top:top + ch, top:top + ch], (zoom_factor, zoom_factor, 1), order=1)
    # trim off any extra pixels
    trim_top = (img.shape[0] - h) // 2

    return img[trim_top:trim_top + h, trim_top:trim_top + h]


# /////////////// End Distortion Methods ///////////////


# /////////////// Distortions ///////////////


#gaussian_noise,shot_noise,impulse_noise,speckle_noise,gaussian_blur,glass_blur,
def gaussian_noise_strong(x, severity=1):
    c = [0.4, 0.6, .8, .9, 1.0][severity - 1]

    x = np.array(x) / 255.
    return np.clip(x + np.random.normal(size=x.shape, scale=c), 0, 1) * 255

def gaussian_noise(x, severity=1):
    c = [.08, .12, 0.18, 0.26, 0.38][severity - 1]

    x = np.array(x) / 255.
    return np.clip(x + np.random.normal(size=x.shape, scale=c), 0, 1) * 255


def shot_noise(x, severity=1):
    c = [60, 25, 12, 5, 3][severity - 1]

    x = np.array(x) / 255.
    return np.clip(np.random.poisson(x * c) / c, 0, 1) * 255


def impulse_noise(x, severity=1):
    c = [.03, .06, .09, 0.17, 0.27][severity - 1]

    x = sk.util.random_noise(np.array(x) / 255., mode='s&p', amount=c)
    return np.clip(x, 0, 1) * 255


def speckle_noise(x, severity=1):
    c = [.15, .2, 0.35, 0.45, 0.6][severity - 1]

    x = np.array(x) / 255.
    return np.clip(x + x * np.random.normal(size=x.shape, scale=c), 0, 1) * 255


def gaussian_blur(x, severity=1):
    c = [1, 2, 3, 4, 6][severity - 1]

    x = gaussian(np.array(x) / 255., sigma=c, multichannel=True)
    return np.clip(x, 0, 1) * 255


def glass_blur(x, severity=1):
    # sigma, max_delta, iterations
    c = [(0.7, 1, 2), (0.9, 2, 1), (1, 2, 3), (1.1, 3, 2), (1.5, 4, 2)][severity - 1]

    x = np.uint8(gaussian(np.array(x) / 255., sigma=c[0], multichannel=True) * 255)

    # locally shuffle pixels
    for i in range(c[2]):
        for h in range(512 - c[1], c[1], -1):
            for w in range(512 - c[1], c[1], -1):
                dx, dy = np.random.randint(-c[1], c[1], size=(2,))
                h_prime, w_prime = h + dy, w + dx
                # swap
                x[h, w], x[h_prime, w_prime] = x[h_prime, w_prime], x[h, w]

    return np.clip(gaussian(x / 255., sigma=c[0], multichannel=True), 0, 1) * 255


def defocus_blur(x, severity=1):
    c = [(3, 0.1), (4, 0.5), (6, 0.5), (8, 0.5), (10, 0.5)][severity - 1]

    x = np.array(x) / 255.
    kernel = disk(radius=c[0], alias_blur=c[1])

    channels = []
    for d in range(3):
        channels.append(cv2.filter2D(x[:, :, d], -1, kernel))
    channels = np.array(channels).transpose((1, 2, 0))  # 3x32x32 -> 32x32x3

    return np.clip(channels, 0, 1) * 255


def motion_blur(x, severity=1):
    c = [(10, 3), (15, 5), (15, 8), (15, 12), (20, 15)][severity - 1]

    output = BytesIO()
    x.save(output, format='PNG')
    x = MotionImage(blob=output.getvalue())

    x.motion_blur(radius=c[0], sigma=c[1], angle=np.random.uniform(-45, 45))

    x = cv2.imdecode(np.fromstring(x.make_blob(), np.uint8),
                     cv2.IMREAD_UNCHANGED)

    if x.shape != (512, 512):
        return np.clip(x[..., [2, 1, 0]], 0, 255)  # BGR to RGB
    else:  # greyscale to RGB
        return np.clip(np.array([x, x, x]).transpose((1, 2, 0)), 0, 255)


def zoom_blur(x, severity=1):
    c = [np.arange(1, 1.11, 0.01),
         np.arange(1, 1.16, 0.01),
         np.arange(1, 1.21, 0.02),
         np.arange(1, 1.26, 0.02),
         np.arange(1, 1.31, 0.03)][severity - 1]
    h,w,_ = np.array(x).shape
    x = (np.array(x) / 255.).astype(np.float32)
    out = np.zeros_like(x)
    for zoom_factor in c:
        clipped_zoom_out = clipped_zoom(x, zoom_factor)
        clipped_zoom_out = cv2.resize(clipped_zoom_out, (w,h))
        out = out + clipped_zoom_out

    x = (x + out) / (len(c) + 1)
    return np.clip(x, 0, 1) * 255

# fog,frost,snow,
def fog(x, severity=1):
    c = [(1.5, 2), (2, 2), (2.5, 1.7), (2.5, 1.5), (3, 1.4)][severity - 1]

    x = np.array(x) / 255.
    max_val = x.max()
    h,w,_ = np.array(x).shape
    x += c[0] *  cv2.resize(plasma_fractal(mapsize=1024, wibbledecay=c[1])[..., np.newaxis],(w,h))[..., np.newaxis]
    return np.clip(x * max_val / (max_val + c[0]), 0, 1) * 255


def frost(x, severity=1):
    c = [(1, 0.4),
         (0.8, 0.6),
         (0.7, 0.7),
         (0.65, 0.7),
         (0.6, 0.75)][severity - 1]
    idx = np.random.randint(5)
    filename = ['./frost/frost1.png', './frost/frost2.png', './frost/frost3.png', './frost/frost4.jpg', './frost/frost5.jpg', './frost/frost6.jpg'][idx]
    frost = cv2.imread(filename)
    #print("frost", frost.shape)
    #frost = cv2.resize(frost, (0, 0), fx=0.2, fy=0.2)
    # randomly crop and convert to rgb
    h,w,_ = np.array(x).shape
    #x_start, y_start = np.random.randint(0, frost.shape[0] - 32), np.random.randint(0, frost.shape[1] - 32)
    #frost = frost[x_start:x_start + 32, y_start:y_start + 32][..., [2, 1, 0]]
    #print("frost.shape",frost.shape)
    frost = cv2.resize(frost, (w,h))
    #print("frost.shape",frost.shape)
    #print("np.array(x)",np.array(x).shape)
    return np.clip(c[0] * np.array(x) + c[1] * frost, 0, 255)


def snow(x, severity=1):
    c = [(0.1, 0.3, 3, 0.5, 10, 4, 0.8),
         (0.2, 0.3, 2, 0.5, 12, 4, 0.7),
         (0.55, 0.3, 4, 0.9, 12, 8, 0.7),
         (0.55, 0.3, 4.5, 0.85, 12, 8, 0.65),
         (0.55, 0.3, 2.5, 0.85, 12, 12, 0.55)][severity - 1]

    x = np.array(x, dtype=np.float32) / 255.
    snow_layer = np.random.normal(size=x.shape[:2], loc=c[0], scale=c[1])  # [:2] for monochrome
    snow_layer = clipped_zoom(snow_layer[..., np.newaxis], c[2])
    snow_layer[snow_layer < c[3]] = 0

    snow_layer = PILImage.fromarray((np.clip(snow_layer.squeeze(), 0, 1) * 255).astype(np.uint8), mode='L')
    output = BytesIO()
    snow_layer.save(output, format='PNG')
    snow_layer = MotionImage(blob=output.getvalue())

    snow_layer.motion_blur(radius=c[4], sigma=c[5], angle=np.random.uniform(-135, -45))

    snow_layer = cv2.imdecode(np.fromstring(snow_layer.make_blob(), np.uint8),
                              cv2.IMREAD_UNCHANGED) / 255.
    snow_layer = snow_layer[..., np.newaxis]
    
    
    h,w,_ = np.array(x).shape
    snow_layer = cv2.resize(snow_layer, (w,h))
    snow_layer = snow_layer[..., np.newaxis]
    x = c[6] * x + (1 - c[6]) * np.maximum(x, cv2.cvtColor(x, cv2.COLOR_RGB2GRAY).reshape(h, w, 1) * 1.5 + 0.5)
    return np.clip(x + snow_layer + np.rot90(snow_layer, k=2), 0, 1) * 255


def spatter(x, severity=1):
    c = [(0.65, 0.3, 4, 0.69, 0.6, 0),
         (0.65, 0.3, 3, 0.68, 0.6, 0),
         (0.65, 0.3, 2, 0.68, 0.5, 0),
         (0.65, 0.3, 1, 0.65, 1.5, 1),
         (0.67, 0.4, 1, 0.65, 1.5, 1)][severity - 1]
    x = np.array(x, dtype=np.float32) / 255.

    liquid_layer = np.random.normal(size=x.shape[:2], loc=c[0], scale=c[1])

    liquid_layer = gaussian(liquid_layer, sigma=c[2])
    liquid_layer[liquid_layer < c[3]] = 0
    if c[5] == 0:
        liquid_layer = (liquid_layer * 255).astype(np.uint8)
        dist = 255 - cv2.Canny(liquid_layer, 50, 150)
        dist = cv2.distanceTransform(dist, cv2.DIST_L2, 5)
        _, dist = cv2.threshold(dist, 20, 20, cv2.THRESH_TRUNC)
        dist = cv2.blur(dist, (3, 3)).astype(np.uint8)
        dist = cv2.equalizeHist(dist)
        #     ker = np.array([[-1,-2,-3],[-2,0,0],[-3,0,1]], dtype=np.float32)
        #     ker -= np.mean(ker)
        ker = np.array([[-2, -1, 0], [-1, 1, 1], [0, 1, 2]])
        dist = cv2.filter2D(dist, cv2.CV_8U, ker)
        dist = cv2.blur(dist, (3, 3)).astype(np.float32)

        m = cv2.cvtColor(liquid_layer * dist, cv2.COLOR_GRAY2BGRA)
        m /= np.max(m, axis=(0, 1))
        m *= c[4]

        # water is pale turqouise
        color = np.concatenate((175 / 255. * np.ones_like(m[..., :1]),
                                238 / 255. * np.ones_like(m[..., :1]),
                                238 / 255. * np.ones_like(m[..., :1])), axis=2)

        color = cv2.cvtColor(color, cv2.COLOR_BGR2BGRA)
        x = cv2.cvtColor(x, cv2.COLOR_BGR2BGRA)

        return cv2.cvtColor(np.clip(x + m * color, 0, 1), cv2.COLOR_BGRA2BGR) * 255
    else:
        m = np.where(liquid_layer > c[3], 1, 0)
        m = gaussian(m.astype(np.float32), sigma=c[4])
        m[m < 0.8] = 0
        #         m = np.abs(m) ** (1/c[4])

        # mud brown
        color = np.concatenate((63 / 255. * np.ones_like(x[..., :1]),
                                42 / 255. * np.ones_like(x[..., :1]),
                                20 / 255. * np.ones_like(x[..., :1])), axis=2)

        color *= m[..., np.newaxis]
        x *= (1 - m[..., np.newaxis])

        return np.clip(x + color, 0, 1) * 255


def contrast(x, severity=1):
    c = [0.4, .3, .2, .1, .05][severity - 1]

    x = np.array(x) / 255.
    means = np.mean(x, axis=(0, 1), keepdims=True)
    return np.clip((x - means) * c + means, 0, 1) * 255


def brightness(x, severity=1):
    c = [.1, .2, .3, .4, .5][severity - 1]

    x = np.array(x) / 255.
    x = sk.color.rgb2hsv(x)
    x[:, :, 2] = np.clip(x[:, :, 2] + c, 0, 1)
    x = sk.color.hsv2rgb(x)

    return np.clip(x, 0, 1) * 255


def saturate(x, severity=1):
    c = [(0.3, 0), (0.1, 0), (2, 0), (5, 0.1), (20, 0.2)][severity - 1]

    x = np.array(x) / 255.
    x = sk.color.rgb2hsv(x)
    x[:, :, 1] = np.clip(x[:, :, 1] * c[0] + c[1], 0, 1)
    x = sk.color.hsv2rgb(x)

    return np.clip(x, 0, 1) * 255

#jpeg_compression,pixelate

def jpeg_compression(x, severity=1):
    c = [25, 18, 15, 10, 7][severity - 1]

    output = BytesIO()
    x.save(output, 'JPEG', quality=c)
    x = PILImage.open(output)

    return x


def pixelate(x, severity=1):
    c = [0.6, 0.5, 0.4, 0.3, 0.25][severity - 1]

    x = x.resize((int(512 * c), int(512 * c)), PILImage.BOX)
    x = x.resize((512, 512), PILImage.BOX)

    return x


# mod of https://gist.github.com/erniejunior/601cdf56d2b424757de5
def elastic_transform(image, severity=1):
    IMSIZE = 512
    c = [(IMSIZE*2, IMSIZE*0.7, IMSIZE*0.1),
         (IMSIZE*2, IMSIZE*0.08, IMSIZE*0.2),
         (IMSIZE*0.05, IMSIZE*0.01, IMSIZE*0.02),
         (IMSIZE*0.07, IMSIZE*0.01, IMSIZE*0.02),
         (IMSIZE*0.12, IMSIZE*0.01, IMSIZE*0.02)][severity - 1]

    image = np.array(image, dtype=np.float32) / 255.
    shape = image.shape
    shape_size = shape[:2]

    # random affine
    center_square = np.float32(shape_size) // 2
    square_size = min(shape_size) // 3
    pts1 = np.float32([center_square + square_size,
                       [center_square[0] + square_size, center_square[1] - square_size],
                       center_square - square_size])
    pts2 = pts1 + np.random.uniform(-c[2], c[2], size=pts1.shape).astype(np.float32)
    M = cv2.getAffineTransform(pts1, pts2)
    image = cv2.warpAffine(image, M, shape_size[::-1], borderMode=cv2.BORDER_REFLECT_101)

    dx = (gaussian(np.random.uniform(-1, 1, size=shape[:2]),
                   c[1], mode='reflect', truncate=3) * c[0]).astype(np.float32)
    dy = (gaussian(np.random.uniform(-1, 1, size=shape[:2]),
                   c[1], mode='reflect', truncate=3) * c[0]).astype(np.float32)
    dx, dy = dx[..., np.newaxis], dy[..., np.newaxis]

    x, y, z = np.meshgrid(np.arange(shape[1]), np.arange(shape[0]), np.arange(shape[2]))
    indices = np.reshape(y + dy, (-1, 1)), np.reshape(x + dx, (-1, 1)), np.reshape(z, (-1, 1))
    return np.clip(map_coordinates(image, indices, order=1, mode='reflect').reshape(shape), 0, 1) * 255

def none(image, severity=1):
    return image
# /////////////// End Distortions ///////////////



# ////////////// Depth Distortion Methods ///////
import cv2
from scipy.ndimage import zoom as scizoom
from scipy.ndimage.interpolation import map_coordinates
import warnings
import random
import numpy as np

warnings.simplefilter("ignore", UserWarning)

def depth_add_gaussian_noise(x, severity=1):
    c = [0.1, 0.2, 0.3, 0.4, 0.5][severity - 1]
    mean = np.mean(x) * c
    std = np.std(x) * c
    noise = np.random.normal(mean, std, x.shape)
    noise = noise.reshape(x.shape).astype('uint16')
    noisy_image = x + noise
    return noisy_image

def depth_add_edge_erosion(x, severity=1):
    c = [(0.015, 3), (0.020, 3), (0.025, 3), (0.03, 3), (0.035, 3)][severity - 1]
    random_rate = c[0]
    patch_len = c[1]
    scaled_x = cv2.normalize(x, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    edges = cv2.Canny(scaled_x, 20, 50)
    gauss = np.full(x.shape, 0,dtype=np.uint16)
    edge_pixel = []
    # Create a mask where edges are 1
    for i in range(edges.shape[0]):
        for j in range(edges.shape[1]):
            if edges[i][j] > 0:
                edge_pixel.append([i,j])
    edge_pixel_num = len(edge_pixel)
    erosion_edge = random.sample(edge_pixel, int(edge_pixel_num * random_rate))
    for pixel in erosion_edge:
        edges[pixel[0] - patch_len:pixel[0] + patch_len, pixel[1] - patch_len:pixel[1] + patch_len] = 1
    edge_mask = edges > 0
    # Apply Gaussian noise only to the edge pixels
    noisy_image = np.copy(x)
    noisy_image[edge_mask] = noisy_image[edge_mask] * gauss[edge_mask]

    return noisy_image

def depth_add_random_mask(x, severity=1):
    c = [5, 7, 9, 11, 13][severity - 1]
    num_rectangles = c
    scale = 0.1
    patch_w = int(x.shape[0] * scale)
    patch_h = int(x.shape[1] * scale)
    mask = np.zeros(x.shape, dtype=np.uint16)
    start_point = []
    sampled_num = 0
    while True:
        x1, y1 = np.random.randint(0, x.shape[0] - patch_w), np.random.randint(0, x.shape[1] - patch_h)
        if len(start_point) == 0:
            start_point.append((x1,y1))
        else:
            for point in start_point:
                if np.abs(point[0] - x1) < patch_w or np.abs(point[1] - y1) < patch_h:
                    continue
                else:
                    start_point.append((x1,y1))
        x2, y2 = x1 + patch_w, y1 + patch_h
        mask[x1:x2,y1:y2] = 1
        sampled_num += 1
        if sampled_num == num_rectangles:
            break
    gauss = np.full(x.shape, 0, dtype=np.uint16)
    noisy_image = np.copy(x)
    mask = mask < 1
    gauss[mask] = 1
    noisy_image = noisy_image * gauss
    return noisy_image

def depth_add_fixed_mask(x, severity=1):
    c = [5, 7, 9, 11, 13][severity - 1]
    scale = 0.1
    patch_w = int(x.shape[0] * scale)
    patch_h = int(x.shape[1] * scale)
    mask = np.zeros(x.shape, dtype=np.uint16)
    start_point = [(1,1),(3,1), (5,1), (7,1),(1,3),(1,5),(1,7),(3,3),(5,5), (9,9),(9,1),(1,9),(7,7)][:c]
    for i in range(c):
        x1, y1 = (start_point[i][0]-1) * patch_w, (start_point[i][1]-1) * patch_h
        x2, y2 = x1 + patch_w, y1 + patch_h
        mask[x1:x2, y1:y2] = 1
    gauss = np.full(x.shape, 0, dtype=np.uint16)
    noisy_image = np.copy(x)
    mask = mask < 1
    gauss[mask] = 1
    noisy_image = noisy_image * gauss
    return noisy_image

def depth_range(x, severity=1):
    c = [(0.2, 3), (0.3, 3.2), (0.4, 3.4), (0.5, 3.6), (0.6, 3.8)][severity - 1]
    mask_sign = 0
    filtered_image = np.copy(x)
    filtered_image = filtered_image / 6553.5
    filtered_image[np.where(np.logical_or(filtered_image > c[1], filtered_image < c[0]))] = mask_sign
    filtered_image = filtered_image * 6553.5
    return filtered_image
# ////////////// End Depth Distortion ///////


# //////////// Data Loading Methods ////////
def load_images_from_folder(dir):
    images = []
    i = 0

    for filename in sorted(os.listdir(dir)):
        # if filename[-3:] == "png":
        #     continue
        if filename[-3:] == "npy": 
            img = np.load(os.path.join(dir,filename))
            # img = np.clip(img, 0, 6)
        else: 
            img = cv2.imread(os.path.join(dir,filename))
        if img is not None:
            images.append(img)
            
        if i % 200 == 0: 
            print(i)
        i += 1
    return images

def load_rgbd_from_folder(dir):
    images = []
    depths = []
    i = 0

    for filename in sorted(os.listdir(dir)):
        img = cv2.imread(os.path.join(dir,filename))
        if img is not None:
            if filename[0:5] == 'frame':
                images.append(img)
            if filename[0:5] == 'depth': 
                depths.append(img)
        if i % 200 == 0: 
            print(i)
        i += 1
    return images, depths

def load_depth_from_folder(dir):
    images = []
    i = 0

    for filename in sorted(os.listdir(dir)):
        if filename[-3:] == "npy":
            img = np.load(os.path.join(dir,filename))
        else:
            img = cv2.imread(os.path.join(dir,filename), cv2.IMREAD_UNCHANGED)
        if img is not None:
            images.append(img)

        if i % 200 == 0:
            print(i)
        i += 1
    return images

def load_timestamps_from_file(stamp_file): 
    stamps = []
    with open(stamp_file) as f:
        for stamp in f:
            stamps.append(float(stamp))
    return stamps
