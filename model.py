import torch
from torch.autograd import Variable
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
#import torchnet
import h5py
import os
import time

### helper functions

def passthrough(x, **kwargs):
    return x

def centre_crop(images, crop_size):
    """
    params:
        images: 5D Variable,
            dim1 --> batch-size
            dim2 --> num_channels
            dim3 x dim4 x dim5 --> 3D image
        crop_size: torch.Size(), 
    return: 
        5D Variable, with same dim1 and dim2 as `images`,
            and each image cropped at the centre down to crop_size
    """
    x, y, z = images[0][0].size()
    cropx = crop_size[2]
    cropy = crop_size[3]
    cropz = crop_size[4]
    assert x >= cropx and y >= cropy and z >= cropz
    startx = x // 2 - (cropx // 2)
    starty = y // 2 - (cropy // 2)
    startz = z // 2 - (cropz // 2)
    return images[:, :, startx:(startx + cropx), starty:(starty + cropy), startz:(startz + cropz)]

#########

### helper classes

class ConvLayer(nn.Module):
    """
    defines a layer consisting of [3D conv]->[3D BatchNorm]->[relu]->
    Convolution: kernel_size = 3x3x3, default_padding = 0
    """
    def __init__(self, 
                in_channels, 
                out_channels, 
                kernel_size=3, 
                stride=1, 
                padding=0):
        super(ConvLayer, self).__init__()
        self.conv = nn.Conv3d(
                        in_channels,
                        out_channels,
                        kernel_size=kernel_size, 
                        stride=stride, 
                        padding=padding)
        self.bn = nn.BatchNorm3d(
                        out_channels, 
                        eps=1e-05, 
                        momentum=0.1, 
                        affine=True)
        self.relu = nn.LeakyReLU()

    def forward(self, x):
        out = self.conv(x)
        out = self.bn(out)
        out = self.relu(out)
        return out
    
class UpConvLayer(nn.Module):
    """
    [3D upconv] -> [3D batchnorm] -> [relu]
    upconv: kernel_size = 2x2x2, stride = 2
    """
    def __init__(self,
                in_channels,
                out_channels):
        super(UpConvLayer, self).__init__()
        self.up_conv = nn.ConvTranspose3d(in_channels, out_channels, kernel_size=2, stride=2)
        self.bn = nn.BatchNorm3d(
                        out_channels, 
                        eps=1e-05, 
                        momentum=0.1, 
                        affine=True)
        self.relu = nn.LeakyReLU()
        
    def forward(self, x):
        out = self.up_conv(x)
        out = self.bn(out)
        out = self.relu(out)
        return out
    
class InputTransition(nn.Module):
    """
    input: one channel
    intermediate: 32 channels
    output: 64 channels
    
    input -> conv -> conv ->
    """
    def __init__(self, n_ch, n_feat):
        super(InputTransition, self).__init__()
        self.conv1 = ConvLayer(n_ch, int(n_feat/2))
        self.conv2 = ConvLayer(int(n_feat/2), n_feat)
        
    def forward(self, x):
        out = self.conv2(self.conv1(x))
        return out

class OutTransition(nn.Module):
    """
    input: 64 channels
    
    input --> 1x1x1 conv --> softmax -> out
    """
    def __init__(self, in_channels, out_channels):
        super(OutTransition, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.conv = nn.Conv3d(in_channels,
                              out_channels, 
                              kernel_size=1, 
                              stride=1, 
                              padding=0)
        self.log_softmax = F.log_softmax
        
    def forward(self, x):
        out = self.conv(x)
        # make channels the last axis
        out = out.permute(0, 2, 3, 4, 1).contiguous()
        # flatten
        out = out.view(out.numel() // self.out_channels, self.out_channels)
        # out = self.log_softmax(out)
        # treat channel 0 as the predicted output
        return out

class DownTransition(nn.Module):
    """
    maxpool -> conv -> conv ->
    """
    def __init__(self, in_channels, if_res_con = False, drop_prob = 0.0):
        super(DownTransition, self).__init__()
        self.max_pool = nn.MaxPool3d(2, stride=2, padding=0)
        self.conv1 = ConvLayer(in_channels, in_channels)
        self.conv2 = ConvLayer(in_channels, 2 * in_channels)
        self.dropout = passthrough
        self.if_res_con = if_res_con
        self.dropout = nn.Dropout3d(p=drop_prob)
        
    def forward(self, x):
        out = self.max_pool(x)
        if self.if_res_con:
            x = self.dropout(out)
            out = self.conv1(x)
            skip_x = centre_crop(x, out.size())
            out = out + skip_x
        else:
            out = self.conv1(out)
        out = self.conv2(out)

        return out
    
class UpTransition(nn.Module):
    """
    upconv -> conv -> conv 
    """
    def __init__(self, in_channels, out_channels, skip_channels, if_res_con = False, drop_prob = 0.0):
        super(UpTransition, self).__init__()
        self.up_conv = UpConvLayer(in_channels, in_channels)
        self.conv1 = ConvLayer(in_channels + skip_channels, out_channels)
        self.conv2 = ConvLayer(out_channels, out_channels)
        self.dropout = passthrough
        self.if_res_con = if_res_con
        self.dropout = nn.Dropout3d(p = drop_prob)
        
    def forward(self, x, skip_x):
        out = self.up_conv(x)
        # crop before doing skip connection
        skip_x = centre_crop(skip_x, out.size())
        out = torch.cat((skip_x, out), 1)
        out = self.dropout(out)
        out = self.conv1(out)
        if self.if_res_con:
            x = out
            out = self.conv2(x)
            skip_out = centre_crop(x, out.size())
            out = skip_out + out
        else:
            out = self.conv2(out)
        return out

##########

### Main class
class UNet(nn.Module):


    
    def __init__(self, n_ch = 1, n_class = 3, num_classes = None, if_bound_refine = False, feat_vec = [64, 128, 256, 512], drop_prob = 0.2):
        super(UNet, self).__init__()
        if num_classes:
            n_class = num_classes
        self.n_class = n_class
        self.inp = InputTransition(n_ch, feat_vec[0])
        self.down1 = DownTransition(feat_vec[0], drop_prob = drop_prob)
        self.down2 = DownTransition(feat_vec[1], drop_prob = drop_prob)
        
        self.down3 = DownTransition(feat_vec[2], drop_prob = drop_prob)
        self.up1 = UpTransition(feat_vec[3], feat_vec[2], feat_vec[2], drop_prob = drop_prob)
        
        self.up2 = UpTransition(feat_vec[2], feat_vec[1], feat_vec[1], drop_prob = drop_prob)
        self.up3 = UpTransition(feat_vec[1], feat_vec[0], feat_vec[0], if_bound_refine, drop_prob = drop_prob)
        
        # final 1x1x1 convolution and softmax
        self.out_trans = OutTransition(feat_vec[0], n_class)
        
        
    def forward(self, x):
        out64 = self.inp(x)
        out128 = self.down1(out64)
        out256 = self.down2(out128) 
        out512 = self.down3(out256)
        out_up256 = self.up1(out512, out256)
              
        out_up128 = self.up2(out_up256, out128)
        out_up64 = self.up3(out_up128, out64)
        
        out = self.out_trans(out_up64)              
        return out

# Discriminator Network
class DiscConvLayer(nn.Module):
    def __init__(self, 
                in_channels, 
                out_channels, 
                kernel_size=3, 
                stride=2, 
                padding=0):
        super(DiscConvLayer, self).__init__()
        self.conv = nn.Conv3d(
                        in_channels,
                        out_channels,
                        kernel_size=kernel_size, 
                        stride=stride, 
                        padding=padding)
        self.bn = nn.BatchNorm3d(
                        out_channels, 
                        eps=1e-05, 
                        momentum=0.1, 
                        affine=True)
        self.leakyrelu = nn.LeakyReLU()

    def forward(self, x):
        out = self.conv(x)
        out = self.bn(out)
        out = self.leakyrelu(out)
        return out

class DiscConvNet(nn.Module):

    def __init__(self, image_shape, n_ch = 1, feat_vec = [32, 64, 128], strides = [2, 2, 1]):
        super(DiscConvNet, self).__init__()
        self.conv1 = DiscConvLayer(n_ch, feat_vec[0])
        self.conv2 = DiscConvLayer(feat_vec[0], feat_vec[1])
        self.flatdim = 600*feat_vec[1]
        self.dense = nn.Linear(self.flatdim, 1)

    def forward(self, x):
        out = self.conv1(x)
        out = self.conv2(out)
        out = out.view(-1, self.flatdim)
        out = self.dense(out)
        return out

class FConvLayer(nn.Module):

    def __init__(self, in_channels, out_channels, kernel_size = 3,stride = 1, padding = 1):
        super(FConvLayer, self).__init__()
        self.conv = nn.Conv3d(in_channels,
                        out_channels,
                        kernel_size=kernel_size, 
                        stride=stride, 
                        padding=padding)
        self.relu = nn.ReLU()
        self.max_pool = MaxPool3d(2, stride=2, padding=0)

    def forward(self, x):
        out = self.conv(x)
        out = self.relu(out)
        out = self.max_pool(out)
        return out

class FullyConv(nn.Module):

    def __init__(self, n_ch = 1, feat_vec = [64, 128, 256, 512, 256, 128, 64]):
        super(FullyConv, self).__init__()
        self.conv1 = FConvLayer(n_ch, feat_vec[0])
        self.conv2 = FConvLayer(feat_vec[0], feat_vec[1])
        self.conv3 = FConvLayer(feat_vec[1], feat_vec[2])
        self.conv4 = FConvLayer(feat_vec[2], feat_vec[3])
        self.conv5 = nn.Conv3d(feat_vec[3], feat_vec[4], kernel_size = 1)

        self.upconv = nn.UpsamplingBilinear3d(scale_factor=2)
        self.conv6 = FConvLayer(feat_vec[2] + feat_vec[4], feat_vec[5])

        #self.upconv2 = nn.UpsamplingBilinear3d(scale_factor=2)
        self.conv7 = FConvLayer(feat_vec[1]+feat_vec[5],feat_vec[6])

        #self.upconv3 = nn.UpsamplingBilinear3d(scale_factor = 2)
        self.conv8 = FConvLayer(feat_vec[0]+feat_vec[6],1)

        #self.upconv4 = nn.UpsamplingBilinear3d(scale_factor = 2)
        self.conv9 = nn.Conv3d(1, 1, kernel_size = 1, padding = 2)

    def forward(self, x):
        out1 = self.conv1(x)
        out2 = self.conv2(out1)
        out3 = self.conv3(out2)
        out4 = self.conv4(out3)
        out5 = self.conv5(out4)

        out = self.upconv(out5)
        out = torch.cat((out, out3),1)
        out = self.conv6(out)
        out = self.upconv(out)
        out = torch.cat((out, out2), 1)
        out = self.conv7(out)
        out = self.upconv(out)
        out = torch.cat((out, out1), 1)
        out = self.conv8(out)
        out = self.upconv(out)
        out = self.conv9(out)

        return out