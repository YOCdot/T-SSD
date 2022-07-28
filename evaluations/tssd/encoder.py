# ---*--- File Information ---*---
# @File       :  templates.py
# @Date&Time  :  2022-07-09, 02:31:55
# @Project    :  swin-ssd
# @Platform   :  Apple Silicon (arm64)
# @Software   :  PyCharm
# @Author     :  yoc
# @Email      :  yyyyyoc@hotmail.com
# codes hacked from Swin-Transformer written by Ze Liu
# https://github.com/microsoft/Swin-Transformer/blob/main/models/swin_transformer.py


import torch
import torch.nn as nn
import torch.utils.checkpoint as checkpoint
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
# from models.layers import DropPath, to_2tuple, trunc_normal_

from typing import List
from torch import Tensor


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


def window_partition(x, window_size):
    """
    Args:
        x: (B, H, W, C)
        window_size (int): window size

    Returns:
        windows: (num_windows*B, window_size, window_size, C)
    """
    B, H, W, C = x.shape
    x = x.view(B, H // window_size, window_size, W // window_size, window_size, C)
    windows = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, C)
    return windows


def window_reverse(windows, window_size, H, W):
    """
    Args:
        windows: (num_windows*B, window_size, window_size, C)
        window_size (int): Window size
        H (int): Height of image
        W (int): Width of image

    Returns:
        x: (B, H, W, C)
    """
    B = int(windows.shape[0] / (H * W / window_size / window_size))
    x = windows.view(B, H // window_size, W // window_size, window_size, window_size, -1)
    x = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, H, W, -1)
    return x


class WindowAttention(nn.Module):
    r""" Window based multi-head self attention (W-MSA) module with relative position bias.
    It supports both of shifted and non-shifted window.

    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The height and width of the window.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set
        attn_drop (float, optional): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
    """

    def __init__(self, dim, window_size, num_heads, qkv_bias=True, qk_scale=None, attn_drop=0., proj_drop=0.):

        super().__init__()
        self.dim = dim
        self.window_size = window_size  # Wh, Ww
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        # define a parameter table of relative position bias
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size[0] - 1) * (2 * window_size[1] - 1), num_heads))  # 2*Wh-1 * 2*Ww-1, nH

        # get pair-wise relative position index for each token inside the window
        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww
        coords_flatten = torch.flatten(coords, 1)  # 2, Wh*Ww
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, Wh*Ww
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wh*Ww, Wh*Ww, 2
        relative_coords[:, :, 0] += self.window_size[0] - 1  # shift to start from 0
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(-1)  # Wh*Ww, Wh*Ww
        self.register_buffer("relative_position_index", relative_position_index)

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        trunc_normal_(self.relative_position_bias_table, std=.02)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, mask=None):
        """
        Args:
            x: input features with shape of (num_windows*B, N, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        B_, N, C = x.shape
        qkv = self.qkv(x).reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # make torchscript happy (cannot use tensor as tuple)

        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))

        relative_position_bias = self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1)  # Wh*Ww,Wh*Ww,nH
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wh*Ww, Wh*Ww
        attn = attn + relative_position_bias.unsqueeze(0)

        if mask is not None:
            nW = mask.shape[0]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

    def extra_repr(self) -> str:
        return f'dim={self.dim}, window_size={self.window_size}, num_heads={self.num_heads}'

    def flops(self, N):
        # calculate flops for 1 window with token length of N
        flops = 0
        # qkv = self.qkv(x)
        flops += N * self.dim * 3 * self.dim
        # attn = (q @ k.transpose(-2, -1))
        flops += self.num_heads * N * (self.dim // self.num_heads) * N
        #  x = (attn @ v)
        flops += self.num_heads * N * N * (self.dim // self.num_heads)
        # x = self.proj(x)
        flops += N * self.dim * self.dim
        return flops


class SwinTransformerBlock(nn.Module):
    r""" Swin Transformer Block.

    Args:
        dim (int): Number of input channels.
        input_resolution (tuple[int]): Input resulotion.
        num_heads (int): Number of attention heads.
        window_size (int): Window size.
        shift_size (int): Shift size for SW-MSA.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float, optional): Stochastic depth rate. Default: 0.0
        act_layer (nn.Module, optional): Activation layer. Default: nn.GELU
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """

    def __init__(self, dim, input_resolution, num_heads, window_size=7, shift_size=0,
                 mlp_ratio=4., qkv_bias=True, qk_scale=None, drop=0., attn_drop=0., drop_path=0.,
                 act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.num_heads = num_heads
        self.window_size = window_size
        self.shift_size = shift_size
        self.mlp_ratio = mlp_ratio
        if min(self.input_resolution) <= self.window_size:
            # if window size is larger than input resolution, we don't partition windows
            self.shift_size = 0
            self.window_size = min(self.input_resolution)
        assert 0 <= self.shift_size < self.window_size, "shift_size must in 0-window_size"

        self.norm1 = norm_layer(dim)
        self.attn = WindowAttention(
            dim, window_size=to_2tuple(self.window_size), num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        if self.shift_size > 0:
            # calculate attention mask for SW-MSA
            H, W = self.input_resolution
            img_mask = torch.zeros((1, H, W, 1))  # 1 H W 1
            h_slices = (slice(0, -self.window_size),
                        slice(-self.window_size, -self.shift_size),
                        slice(-self.shift_size, None))
            w_slices = (slice(0, -self.window_size),
                        slice(-self.window_size, -self.shift_size),
                        slice(-self.shift_size, None))
            cnt = 0
            for h in h_slices:
                for w in w_slices:
                    img_mask[:, h, w, :] = cnt
                    cnt += 1

            mask_windows = window_partition(img_mask, self.window_size)  # nW, window_size, window_size, 1
            mask_windows = mask_windows.view(-1, self.window_size * self.window_size)
            attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
            attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(attn_mask == 0, float(0.0))
        else:
            attn_mask = None

        self.register_buffer("attn_mask", attn_mask)

    def forward(self, x):
        H, W = self.input_resolution
        B, L, C = x.shape
        assert L == H * W, "input feature has wrong size"

        shortcut = x
        x = self.norm1(x)
        x = x.view(B, H, W, C)

        # cyclic shift
        if self.shift_size > 0:
            shifted_x = torch.roll(x, shifts=(-self.shift_size, -self.shift_size), dims=(1, 2))
        else:
            shifted_x = x

        # partition windows
        x_windows = window_partition(shifted_x, self.window_size)  # nW*B, window_size, window_size, C
        x_windows = x_windows.view(-1, self.window_size * self.window_size, C)  # nW*B, window_size*window_size, C

        # W-MSA/SW-MSA
        attn_windows = self.attn(x_windows, mask=self.attn_mask)  # nW*B, window_size*window_size, C

        # merge windows
        attn_windows = attn_windows.view(-1, self.window_size, self.window_size, C)
        shifted_x = window_reverse(attn_windows, self.window_size, H, W)  # B H' W' C

        # reverse cyclic shift
        if self.shift_size > 0:
            x = torch.roll(shifted_x, shifts=(self.shift_size, self.shift_size), dims=(1, 2))
        else:
            x = shifted_x
        x = x.view(B, H * W, C)
        x = shortcut + self.drop_path(x)

        # FFN
        x = x + self.drop_path(self.mlp(self.norm2(x)))

        return x

    def extra_repr(self) -> str:
        return f"dim={self.dim}, input_resolution={self.input_resolution}, num_heads={self.num_heads}, " \
               f"window_size={self.window_size}, shift_size={self.shift_size}, mlp_ratio={self.mlp_ratio}"

    def flops(self):
        flops = 0
        H, W = self.input_resolution
        # norm1
        flops += self.dim * H * W
        # W-MSA/SW-MSA
        nW = H * W / self.window_size / self.window_size
        flops += nW * self.attn.flops(self.window_size * self.window_size)
        # mlp
        flops += 2 * H * W * self.dim * self.dim * self.mlp_ratio
        # norm2
        flops += self.dim * H * W
        return flops


class PatchMerging(nn.Module):
    r""" Patch Merging Layer.

    Args:
        input_resolution (tuple[int]): Resolution of input feature.
        dim (int): Number of input channels.
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """

    def __init__(self, input_resolution, dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.input_resolution = input_resolution
        self.dim = dim
        self.reduction = nn.Linear(4 * dim, 2 * dim, bias=False)
        self.norm = norm_layer(4 * dim)

    def forward(self, x):
        """
        x: B, H*W, C
        """
        H, W = self.input_resolution
        B, L, C = x.shape
        assert L == H * W, "input feature has wrong size"
        assert H % 2 == 0 and W % 2 == 0, f"x size ({H}*{W}) are not even."

        x = x.view(B, H, W, C)

        x0 = x[:, 0::2, 0::2, :]  # B H/2 W/2 C
        x1 = x[:, 1::2, 0::2, :]  # B H/2 W/2 C
        x2 = x[:, 0::2, 1::2, :]  # B H/2 W/2 C
        x3 = x[:, 1::2, 1::2, :]  # B H/2 W/2 C
        x = torch.cat([x0, x1, x2, x3], -1)  # B H/2 W/2 4*C
        x = x.view(B, -1, 4 * C)  # B H/2*W/2 4*C

        x = self.norm(x)
        x = self.reduction(x)

        return x

    def extra_repr(self) -> str:
        return f"input_resolution={self.input_resolution}, dim={self.dim}"

    def flops(self):
        H, W = self.input_resolution
        flops = H * W * self.dim
        flops += (H // 2) * (W // 2) * 4 * self.dim * 2 * self.dim
        return flops


class BasicLayer(nn.Module):
    """ A basic Swin Transformer layer for one stage.

    Args:
        dim (int): Number of input channels.
        input_resolution (tuple[int]): Input resolution.
        depth (int): Number of blocks.
        num_heads (int): Number of attention heads.
        window_size (int): Local window size.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
        norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
        downsample (nn.Module | None, optional): Downsample layer at the end of the layer. Default: None
        use_checkpoint (bool): Whether to use checkpointing to save memory. Default: False.
    """

    def __init__(self, dim, input_resolution, depth, num_heads, window_size,
                 mlp_ratio=4., qkv_bias=True, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., norm_layer=nn.LayerNorm, downsample=None, use_checkpoint=False):

        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.depth = depth
        self.use_checkpoint = use_checkpoint

        # build blocks
        self.blocks = nn.ModuleList([
            SwinTransformerBlock(dim=dim,
                                 input_resolution=input_resolution,
                                 num_heads=num_heads,
                                 window_size=window_size,
                                 shift_size=0 if (i % 2 == 0) else window_size // 2,
                                 mlp_ratio=mlp_ratio,
                                 qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop, attn_drop=attn_drop,
                                 drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path,
                                 norm_layer=norm_layer)
            for i in range(depth)])

        # patch merging layer
        if downsample is not None:
            self.downsample = downsample(input_resolution, dim=dim, norm_layer=norm_layer)
        else:
            self.downsample = None

    def forward(self, x):
        for blk in self.blocks:
            if self.use_checkpoint:
                x = checkpoint.checkpoint(blk, x)
            else:
                x = blk(x)
        if self.downsample is not None:
            x = self.downsample(x)
        return x

    def extra_repr(self) -> str:
        return f"dim={self.dim}, input_resolution={self.input_resolution}, depth={self.depth}"

    def flops(self):
        flops = 0
        for blk in self.blocks:
            flops += blk.flops()
        if self.downsample is not None:
            flops += self.downsample.flops()
        return flops


class PatchEmbed(nn.Module):
    r""" Image to Patch Embedding

    Args:
        img_size (int): Image size.  Default: 224.
        patch_size (int): Patch token size. Default: 4.
        in_chans (int): Number of input image channels. Default: 3.
        embed_dim (int): Number of linear projection output channels. Default: 96.
        norm_layer (nn.Module, optional): Normalization layer. Default: None
    """

    def __init__(self, img_size=224, patch_size=4, in_chans=3, embed_dim=96, norm_layer=None):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        patches_resolution = [img_size[0] // patch_size[0], img_size[1] // patch_size[1]]
        self.img_size = img_size
        self.patch_size = patch_size
        self.patches_resolution = patches_resolution
        self.num_patches = patches_resolution[0] * patches_resolution[1]

        self.in_chans = in_chans
        self.embed_dim = embed_dim

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

    def forward(self, x):
        B, C, H, W = x.shape
        # FIXME look at relaxing size constraints
        assert H == self.img_size[0] and W == self.img_size[1], \
            f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x).flatten(2).transpose(1, 2)  # B Ph*Pw C
        if self.norm is not None:
            x = self.norm(x)
        return x

    def flops(self):
        Ho, Wo = self.patches_resolution
        flops = Ho * Wo * self.embed_dim * self.in_chans * (self.patch_size[0] * self.patch_size[1])
        if self.norm is not None:
            flops += Ho * Wo * self.embed_dim
        return flops


class SwinTransformer(nn.Module):
    r""" Swin Transformer
        A PyTorch impl of : `Swin Transformer: Hierarchical Vision Transformer using Shifted Windows`  -
          https://arxiv.org/pdf/2103.14030

    Args:
        img_size (int | tuple(int)): Input image size. Default 224
        patch_size (int | tuple(int)): Patch size. Default: 4
        in_chans (int): Number of input image channels. Default: 3
        num_classes (int): Number of classes for classification head. Default: 1000
        embed_dim (int): Patch embedding dimension. Default: 96
        depths (tuple(int)): Depth of each Swin Transformer layer.
        num_heads (tuple(int)): Number of attention heads in different layers.
        window_size (int): Window size. Default: 7
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4
        qkv_bias (bool): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float): Override default qk scale of head_dim ** -0.5 if set. Default: None
        drop_rate (float): Dropout rate. Default: 0
        attn_drop_rate (float): Attention dropout rate. Default: 0
        drop_path_rate (float): Stochastic depth rate. Default: 0.1
        norm_layer (nn.Module): Normalization layer. Default: nn.LayerNorm.
        ape (bool): If True, add absolute position embedding to the patch embedding. Default: False
        patch_norm (bool): If True, add normalization after patch embedding. Default: True
        use_checkpoint (bool): Whether to use checkpointing to save memory. Default: False
    """

    def __init__(self, img_size=224, patch_size=4, in_chans=3, num_classes=1000,
                 embed_dim=96, depths=[2, 2, 6, 2], num_heads=[3, 6, 12, 24],
                 window_size=7, mlp_ratio=4., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.1,
                 norm_layer=nn.LayerNorm, ape=False, patch_norm=True,
                 use_checkpoint=False, **kwargs):
        super().__init__()

        self.num_classes = num_classes
        self.num_layers = len(depths)
        self.embed_dim = embed_dim
        self.ape = ape
        self.patch_norm = patch_norm
        self.num_features = int(embed_dim * 2 ** (self.num_layers - 1))
        self.mlp_ratio = mlp_ratio

        # split image into non-overlapping patches
        self.patch_embed = PatchEmbed(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim,
            norm_layer=norm_layer if self.patch_norm else None)
        num_patches = self.patch_embed.num_patches
        patches_resolution = self.patch_embed.patches_resolution
        self.patches_resolution = patches_resolution

        # absolute position embedding
        if self.ape:
            self.absolute_pos_embed = nn.Parameter(torch.zeros(1, num_patches, embed_dim))
            trunc_normal_(self.absolute_pos_embed, std=.02)

        self.pos_drop = nn.Dropout(p=drop_rate)

        # stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule

        # build layers
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(dim=int(embed_dim * 2 ** i_layer),
                               input_resolution=(patches_resolution[0] // (2 ** i_layer),
                                                 patches_resolution[1] // (2 ** i_layer)),
                               depth=depths[i_layer],
                               num_heads=num_heads[i_layer],
                               window_size=window_size,
                               mlp_ratio=self.mlp_ratio,
                               qkv_bias=qkv_bias, qk_scale=qk_scale,
                               drop=drop_rate, attn_drop=attn_drop_rate,
                               drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                               norm_layer=norm_layer,
                               downsample=PatchMerging if (i_layer < self.num_layers - 1) else None,
                               use_checkpoint=use_checkpoint)
            self.layers.append(layer)

        self.norm = norm_layer(self.num_features)
        self.avgpool = nn.AdaptiveAvgPool1d(1)
        self.head = nn.Linear(self.num_features, num_classes) if num_classes > 0 else nn.Identity()

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table'}

    def forward_features(self, x):
        x = self.patch_embed(x)
        if self.ape:
            x = x + self.absolute_pos_embed
        x = self.pos_drop(x)

        for layer in self.layers:
            x = layer(x)

        x = self.norm(x)  # B L C
        x = self.avgpool(x.transpose(1, 2))  # B C 1
        x = torch.flatten(x, 1)
        return x

    def forward(self, x):
        x = self.forward_features(x)
        x = self.head(x)
        return x

    def flops(self):
        flops = 0
        flops += self.patch_embed.flops()
        for i, layer in enumerate(self.layers):
            flops += layer.flops()
        flops += self.num_features * self.patches_resolution[0] * self.patches_resolution[1] // (2 ** self.num_layers)
        flops += self.num_features * self.num_classes
        return flops


class Encoder(nn.Module):
    """ SwinT based encoder: without PatchEmbed & Heads"""

    def __init__(self, img_size=224, patch_size=4, in_chans=3, num_classes=1000,
                 embed_dim=96, depths=[2, 2, 6, 2], num_heads=[3, 6, 12, 24],
                 window_size=7, mlp_ratio=4., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.1,
                 norm_layer=nn.LayerNorm, ape=False, patch_norm=True,
                 use_checkpoint=False,
                 dim_list=[192, 384, 768, 768], num_list=[150, 150, 150, 150], **kwargs):
        super().__init__()

        self.img_size = img_size
        self.num_layers = len(depths)
        self.embed_dim = embed_dim
        self.ape = ape
        self.patch_norm = patch_norm
        self.num_features = int(embed_dim * 2 ** (self.num_layers - 1))
        self.mlp_ratio = mlp_ratio

        self.dim_list = dim_list
        self.num_list = num_list

        # split image into non-overlapping patches
        self.patch_embed = PatchEmbed(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim,
            norm_layer=norm_layer if self.patch_norm else None)
        num_patches = self.patch_embed.num_patches
        patches_resolution = self.patch_embed.patches_resolution
        self.patches_resolution = patches_resolution

        # absolute position embedding
        if self.ape:
            self.absolute_pos_embed = nn.Parameter(torch.zeros(1, num_patches, embed_dim))
            trunc_normal_(self.absolute_pos_embed, std=.02)

        self.pos_drop = nn.Dropout(p=drop_rate)

        # stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule

        # build layers
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(dim=int(embed_dim * 2 ** i_layer),
                               input_resolution=(patches_resolution[0] // (2 ** i_layer),
                                                 patches_resolution[1] // (2 ** i_layer)),
                               depth=depths[i_layer],
                               num_heads=num_heads[i_layer],
                               window_size=window_size,
                               mlp_ratio=self.mlp_ratio,
                               qkv_bias=qkv_bias, qk_scale=qk_scale,
                               drop=drop_rate, attn_drop=attn_drop_rate,
                               drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                               norm_layer=norm_layer,
                               downsample=PatchMerging if (i_layer < self.num_layers - 1) else None,
                               use_checkpoint=use_checkpoint)
            self.layers.append(layer)

        # TODO: added
        self.norm_list = nn.ModuleList([])
        for dim in self.dim_list:
            self.norm_list.append(norm_layer(dim))
        # TODO: added
        self.pool_list = nn.ModuleList([])
        if num_list[0] == 169:
            self.pool_list.append(nn.AvgPool2d(kernel_size=7, stride=6))  # sqrt(7056): 84 -> 13
            self.pool_list.append(nn.AvgPool2d(kernel_size=5, stride=3))  # sqrt(1764): 42 -> 13
            self.pool_list.append(nn.AvgPool2d(kernel_size=9, stride=1))  # sqrt(441): 21 -> 13
            self.pool_list.append(nn.AvgPool2d(kernel_size=9, stride=1))  # sqrt(441): 21 -> 13
        elif num_list[0] == 225:
            self.pool_list.append(nn.AvgPool2d(kernel_size=10, stride=5))  # sqrt(7056): 84 -> 15
            self.pool_list.append(nn.AvgPool2d(kernel_size=13, stride=2))  # sqrt(1764): 42 -> 15
            self.pool_list.append(nn.AvgPool2d(kernel_size=7, stride=1))  # sqrt(441): 21 -> 15
            self.pool_list.append(nn.AvgPool2d(kernel_size=7, stride=1))  # sqrt(441): 21 -> 15
        elif num_list[0] == 289:
            self.pool_list.append(nn.AvgPool2d(kernel_size=12, stride=5))  # sqrt(9216): 96 -> 17
            self.pool_list.append(nn.AvgPool2d(kernel_size=15, stride=2))  # sqrt(2304): 48 -> 17
            self.pool_list.append(nn.AvgPool2d(kernel_size=8, stride=1))  # sqrt(576): 24 -> 17
            self.pool_list.append(nn.AvgPool2d(kernel_size=8, stride=1))  # sqrt(576): 24 -> 17

        # self.pool_list = nn.ModuleList([])
        # for n in self.num_list:
        #     self.pool_list.append(nn.AdaptiveAvgPool1d(n))

        # TODO: anno
        # self.norm = norm_layer(self.num_features)
        # self.avgpool = nn.AdaptiveAvgPool1d(1)
        # ------

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table'}

    def forward_features(self, x):
        x = self.patch_embed(x)
        if self.ape:
            x = x + self.absolute_pos_embed
        x = self.pos_drop(x)

        # for layer in self.layers:
        #     x = layer(x)
        #     print(f"after sw layer: {x.size()}")
        # exit()
        # x = self.norm(x)  # B L C
        # x = self.avgpool(x.transpose(1, 2))  # B C 1
        # x = torch.flatten(x, 1)
        # return x

        out_list = torch.jit.annotate(List[Tensor], [])
        for layer, norm, pool in zip(self.layers, self.norm_list, self.pool_list):
            x = layer(x)
            t = norm(x).transpose(1, 2)
            # t = rearrange(t, "b d (e1 e2) -> b d e1 e2", e1=e, e2=e)
            # t = rearrange(pool(t), "b d e1 e2 -> b (e1 e2) d")
            B, D, EE = t.shape
            E = int(EE ** 0.5)
            t = t.view(B, D, E, E)
            t = pool(t).flatten(2).transpose(1, 2)
            # print(t.size())
            # t = pool(t).transpose(1, 2)
            out_list.append(t)

        # for t in out_list:
        #     print(t.size())

        return out_list

    def forward(self, x):
        x = self.forward_features(x)
        return x

    def flops(self):
        flops = 0
        flops += self.patch_embed.flops()
        for i, layer in enumerate(self.layers):
            flops += layer.flops()
        flops += self.num_features * self.patches_resolution[0] * self.patches_resolution[1] // (2 ** self.num_layers)
        flops += self.num_features * self.num_classes
        return flops


def swin_tiny_patch4_window7_224_22kto1k_finetune(pretrain=None):
    """
    MODEL:
        TYPE: swin
        NAME: swin_tiny_patch4_window7_224_22kto1k_finetune
        DROP_PATH_RATE: 0.1
        SWIN:
            EMBED_DIM: 96
            DEPTHS: [ 2, 2, 6, 2 ]
            NUM_HEADS: [ 3, 6, 12, 24 ]
            WINDOW_SIZE: 7
    TRAIN:
        EPOCHS: 30
        WARMUP_EPOCHS: 5
        WEIGHT_DECAY: 1e-8
        BASE_LR: 2e-05
        WARMUP_LR: 2e-08
        MIN_LR: 2e-07
    """
    model = SwinTransformer(img_size=224,
                            num_classes=1000,
                            embed_dim=96, depths=[2, 2, 6, 2], num_heads=[3, 6, 12, 24], window_size=7,
                            drop_path_rate=.1)
    if pretrain:
        checkpoint = torch.load(pretrain, map_location='cpu')
        model.load_state_dict(checkpoint["model"])
        print("`swin_tiny_patch4_window7_224_22kto1k_finetune` pre-trained weights loaded!")
    return model


def swin_small_patch4_window7_224_22kto1k_finetune(pretrain=None):
    """
    MODEL:
        TYPE: swin
        NAME: swin_small_patch4_window7_224_22kto1k_finetune
        DROP_PATH_RATE: 0.2
        SWIN:
            EMBED_DIM: 96
            DEPTHS: [ 2, 2, 18, 2 ]
            NUM_HEADS: [ 3, 6, 12, 24 ]
            WINDOW_SIZE: 7
    TRAIN:
        EPOCHS: 30
        WARMUP_EPOCHS: 5
        WEIGHT_DECAY: 1e-8
        BASE_LR: 2e-05
        WARMUP_LR: 2e-08
        MIN_LR: 2e-07
    """

    model = SwinTransformer(img_size=224,
                            num_classes=1000,
                            embed_dim=96, depths=[2, 2, 18, 2], num_heads=[3, 6, 12, 24], window_size=7,
                            drop_path_rate=.2)
    if pretrain:
        checkpoint = torch.load(pretrain, map_location='cpu')
        model.load_state_dict(checkpoint["model"])
        print("`swin_small_patch4_window7_224_22kto1k_finetune` pre-trained weights loaded!")
    return model


def swin_base_patch4_window12_384_22kto1k_finetune(pretrain=None):
    """
    DATA:
        IMG_SIZE: 384
    MODEL:
        TYPE: swin
        NAME: swin_base_patch4_window12_384_22kto1k_finetune
        DROP_PATH_RATE: 0.2
        SWIN:
            EMBED_DIM: 128
            DEPTHS: [ 2, 2, 18, 2 ]
            NUM_HEADS: [ 4, 8, 16, 32 ]
            WINDOW_SIZE: 12
    TRAIN:
        EPOCHS: 30
        WARMUP_EPOCHS: 5
        WEIGHT_DECAY: 1e-8
        BASE_LR: 2e-05
        WARMUP_LR: 2e-08
        MIN_LR: 2e-07
    TEST:
        CROP: False
    """
    model = SwinTransformer(img_size=384,
                            num_classes=1000,
                            embed_dim=128, depths=[2, 2, 18, 2], num_heads=[4, 8, 16, 32], window_size=12,
                            drop_path_rate=.2)
    if pretrain:
        checkpoint = torch.load(pretrain, map_location='cpu')
        model.load_state_dict(checkpoint["model"])
        print("`swin_base_patch4_window12_384_22kto1k_finetune` pre-trained weights loaded!")
    return model


def encoder_tiny_672_192(pretrain=None):
    dim_list = [192, 384, 768, 768]
    num_list = [169, 169, 169, 169]
    encoder = Encoder(img_size=672,
                      num_classes=0,
                      embed_dim=96, depths=[2, 2, 6, 2], num_heads=[3, 6, 12, 24], window_size=7,
                      drop_path_rate=.1,
                      dim_list=dim_list, num_list=num_list, name="tiny")
    if pretrain:
        checkpoint = torch.load(pretrain, map_location='cpu')
        pop_list = []
        for k in checkpoint["model"].keys():
            if str(k).split(".")[-1] == "attn_mask":
                pop_list.append(k)
        for p in pop_list:
            # print(p)
            checkpoint["model"].pop(p)
        encoder.load_state_dict(checkpoint["model"], strict=False)
        print("encoder_tiny_672_192 built!\n"
              "\tWeights: `swin_tiny_patch4_window7_224_22kto1k_finetune` loaded!")

    return encoder


def encoder_small_672_384(pretrain=None):
    dim_list = [192, 384, 768, 768]
    num_list = [225, 225, 225, 225]
    encoder = Encoder(img_size=672,
                      num_classes=0,
                      embed_dim=96, depths=[2, 2, 18, 2], num_heads=[3, 6, 12, 24], window_size=7,
                      drop_path_rate=.2,
                      dim_list=dim_list, num_list=num_list)
    if pretrain:
        checkpoint = torch.load(pretrain, map_location='cpu')
        pop_list = []
        for k in checkpoint["model"].keys():
            if str(k).split(".")[-1] == "attn_mask":
                pop_list.append(k)
        for p in pop_list:
            checkpoint["model"].pop(p)
        encoder.load_state_dict(checkpoint["model"], strict=False)
        print("encoder_small_672_384 built!\n"
              "\tWeights: `swin_small_patch4_window7_224_22kto1k_finetune` loaded!")

    return encoder


def encoder_base_768_768(pretrain=None):
    dim_list = [256, 512, 1024, 1024]
    num_list = [289, 289, 289, 289]
    encoder = Encoder(img_size=768,
                      num_classes=0,
                      embed_dim=128, depths=[2, 2, 18, 2], num_heads=[4, 8, 16, 32], window_size=12,
                      drop_path_rate=.2,
                      dim_list=dim_list, num_list=num_list)
    if pretrain:
        checkpoint = torch.load(pretrain, map_location='cpu')
        pop_list = []
        for k in checkpoint["model"].keys():
            if str(k).split(".")[-1] == "attn_mask":
                pop_list.append(k)
        for p in pop_list:
            # print(p)
            checkpoint["model"].pop(p)
        encoder.load_state_dict(checkpoint["model"], strict=False)
        print("encoder_small_768_768 built!\n"
              "\tWeights: `swin_base_patch4_window12_384_22kto1k_finetune` loaded!")

    return encoder


def get_parameter_number(net):
    total_num = sum(p.numel() for p in net.parameters())
    trainable_num = sum(p.numel() for p in net.parameters() if p.requires_grad)
    return {'Total': total_num, 'Trainable': trainable_num}


if __name__ == "__main__":
    device = "cuda" if torch.cuda.is_available() else "cpu"
    # device = "mps" if torch.backends.mps.is_available() else "cpu"
    # device = "mps" if (getattr(torch.backends, "mps",
    #                            None) is not None) and torch.backends.mps.is_available() else "cpu"
    # print(f"参数量： {get_parameter_number(encoder)['Total']}, 可训练{get_parameter_number(encoder)['Trainable']}")

    import cv2
    import torchvision.transforms as transforms
    img = cv2.imread('../train_image.jpg')
    # INPUT-224
    trans224 = transforms.Compose([transforms.ToTensor(),
                                   transforms.Resize((224, 224))])  # (480, 640, 3) -> (3, 224, 224)
    input224 = trans224(img)  # tensor数据格式是torch(C,H,W)
    input224 = input224.unsqueeze(0)
    input224_b = input224
    input224 = torch.cat((input224, input224_b), dim=0)
    input224 = input224.to(device)
    # INPUT-384
    trans384 = transforms.Compose([transforms.ToTensor(),
                                   transforms.Resize((384, 384))])  # (480, 640, 3) -> (3, 224, 224)
    input384 = trans384(img)  # tensor数据格式是torch(C,H,W)
    input384 = input384.unsqueeze(0)
    input384_b = input384
    input384 = torch.cat((input384, input384_b), dim=0)
    input384 = input384.to(device)
    # INPUT-672
    trans672 = transforms.Compose([transforms.ToTensor(),
                                   transforms.Resize((672, 672))])  # (480, 640, 3) -> (3, 224, 224)
    input672 = trans672(img)  # tensor数据格式是torch(C,H,W)
    input672 = input672.unsqueeze(0)
    input672_b = input672
    input672 = torch.cat((input672, input672_b), dim=0)
    input672 = input672.to(device)
    # INPUT-768
    trans768 = transforms.Compose([transforms.ToTensor(),
                                   transforms.Resize((768, 768))])  # (480, 640, 3) -> (3, 224, 224)
    input768 = trans768(img)  # tensor数据格式是torch(C,H,W)
    input768 = input768.unsqueeze(0)
    input768_b = input768
    input768 = torch.cat((input768, input768_b), dim=0)
    input768 = input768.to(device)

    # FIXME: model zoo
    swin_tiny_224 = swin_tiny_patch4_window7_224_22kto1k_finetune(
        pretrain="/Users/iyoc/ProjectFiles/SwinWeights/swin_tiny_patch4_window7_224_22kto1k_finetune.pth"
    )
    swin_tiny_output = swin_tiny_224(input224)
    print(f"swin-tiny: {swin_tiny_output.size()}")

    swin_small_224 = swin_small_patch4_window7_224_22kto1k_finetune(
        pretrain="/Users/iyoc/ProjectFiles/SwinWeights/swin_small_patch4_window7_224_22kto1k_finetune.pth"
    )
    swin_small_output224 = swin_small_224(input224)
    print(f"swin-small: {swin_small_output224.size()}")

    swin_base_384 = swin_base_patch4_window12_384_22kto1k_finetune(
        pretrain="/Users/iyoc/ProjectFiles/SwinWeights/swin_base_patch4_window12_384_22kto1k.pth"
    )
    swin_base_output384 = swin_base_384(input384)
    print(f"swin-base: {swin_base_output384.size()}")

    # FIXME: encoder zoo
    encoder_tiny = encoder_tiny_672_192(
        pretrain="/Users/iyoc/ProjectFiles/SwinWeights/swin_tiny_patch4_window7_224_22kto1k_finetune.pth"
    )
    encoder_tiny_output = encoder_tiny(input672)
    print("encoder-tiny:", [t.size() for t in encoder_tiny_output])

    encoder_small = encoder_small_672_384(
        pretrain="/Users/iyoc/ProjectFiles/SwinWeights/swin_small_patch4_window7_224_22kto1k_finetune.pth"
    )
    encoder_small_output = encoder_small(input672)
    print("encoder-small:", [t.size() for t in encoder_small_output])

    encoder_base = encoder_base_768_768(
        pretrain="/Users/iyoc/ProjectFiles/SwinWeights/swin_base_patch4_window12_384_22kto1k.pth"
    )
    encoder_base_output = encoder_base(input768)
    print("encoder-base:", [t.size() for t in encoder_base_output])

