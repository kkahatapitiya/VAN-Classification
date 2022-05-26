import torch
import torch.nn as nn
import torch.nn.functional as F
from functools import partial

from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from timm.models.registry import register_model
from timm.models.vision_transformer import _cfg
import math
from einops import rearrange


class DWConv(nn.Module):
    def __init__(self, dim=768, layer=0):
        super(DWConv, self).__init__()
        self.ks = {0:5, 1:5, 2:5, 3:3}[layer]
        self.dwconv = nn.Conv2d(dim, dim, self.ks, padding=self.ks//2, bias=True, groups=dim)

    def forward(self, x):
        x = self.dwconv(x)
        return x


def _conv_filter(state_dict, patch_size=16):
    """ convert patch embedding weight from manual patchify + linear proj to conv"""
    out_dict = {}
    for k, v in state_dict.items():
        if 'patch_embed.proj.weight' in k:
            v = v.reshape((v.shape[0], 3, patch_size, patch_size))
        out_dict[k] = v

    return out_dict


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0., layer=0):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Conv2d(in_features, hidden_features, 1)
        self.dwconv = DWConv(hidden_features, layer)
        self.act = act_layer()
        self.fc2 = nn.Conv2d(hidden_features, out_features, 1)
        self.drop = nn.Dropout(drop)

        self.layer = layer
        if self.layer>1:
            self.norm1 = nn.BatchNorm2d(hidden_features)
        self.norm2 = nn.BatchNorm2d(hidden_features)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x):
        #x = self.act(self.norm1(self.fc1(x)))
        if self.layer>1:
            x = self.act(self.norm1(self.fc1(x)))
        else:
            x = self.fc1(x)
        x = self.act(self.norm2(self.dwconv(x)))
        #x = self.act(x)
        #x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x




class LKA(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.conv0 = nn.Conv2d(dim, dim, 5, padding=2, groups=dim)
        self.conv_spatial = nn.Conv2d(dim, dim, 7, stride=1, padding=9, groups=dim, dilation=3)
        self.conv1 = nn.Conv2d(dim, dim, 1)


    def forward(self, x):
        u = x.clone()
        attn = self.conv0(x)
        attn = self.conv_spatial(attn)
        attn = self.conv1(attn)

        return u * attn


class Attention(nn.Module):
    def __init__(self, d_model, layer):
        super().__init__()

        self.layer = layer
        self.proj_1 = nn.Conv2d(d_model, d_model, 1)
        self.activation = nn.GELU()
        self.spatial_gating_unit = LKA(d_model)
        self.proj_2 = nn.Conv2d(d_model, d_model, 1)

        self.c, self.h, self.w, self.ks = {0:(16,2,2,1), 1:(8,4,4,3), 2:(5,8,8,5), 3:(2,16,16,7)}[layer]
        self.proj_1c = nn.Conv2d(self.c, self.c, self.ks, padding=self.ks//2)
        #self.proj_2c = nn.Conv2d(self.c, self.c, self.ks, padding=self.ks//2)

    def forward(self, x):
        # 26.6M 5.0G
        # model                        | 26.93M                 | 5.669G
        # model                        | 25.105M                | 5.439G
        shorcut = x.clone()
        x_ = rearrange(x, 'b (c p1 p2) n1 n2 -> b c (n1 p1) (n2 p2)', p1=self.h, p2=self.w)
        x_ = self.proj_1c(x_)
        x_ = rearrange(x_, 'b c (n1 p1) (n2 p2) -> b (c p1 p2) n1 n2', p1=self.h, p2=self.w)
        x = 0.5 * (self.proj_1(x) + x_)
        #x = self.proj_1(x)
        x = self.activation(x)

        x = self.spatial_gating_unit(x)

        '''x_ = rearrange(x, 'b (c p1 p2) n1 n2 -> b c (n1 p1) (n2 p2)', p1=self.h, p2=self.w)
        x_ = self.proj_2c(x_)
        x_ = rearrange(x_, 'b c (n1 p1) (n2 p2) -> b (c p1 p2) n1 n2', p1=self.h, p2=self.w)
        x = 0.5 * (self.proj_2(x) + x_)'''
        x = self.proj_2(x)

        x = x + shorcut
        return x


class Block(nn.Module):
    def __init__(self, dim, mlp_ratio=4., drop=0.,drop_path=0., layer=0, act_layer=nn.GELU):
        super().__init__()
        self.norm1 = nn.BatchNorm2d(dim)
        self.attn = Attention(dim, layer)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.norm2 = nn.BatchNorm2d(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop, layer=layer)
        layer_scale_init_value = 1e-2
        self.layer_scale_1 = nn.Parameter(
            layer_scale_init_value * torch.ones((dim)), requires_grad=True)
        self.layer_scale_2 = nn.Parameter(
            layer_scale_init_value * torch.ones((dim)), requires_grad=True)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x):
        x = x + self.drop_path(self.layer_scale_1.unsqueeze(-1).unsqueeze(-1) * self.attn(self.norm1(x)))
        x = x + self.drop_path(self.layer_scale_2.unsqueeze(-1).unsqueeze(-1) * self.mlp(self.norm2(x)))
        return x


class OverlapPatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """

    def __init__(self, img_size=224, patch_size=7, stride=4, in_chans=3, embed_dim=768, layer=0):
        super().__init__()
        patch_size = to_2tuple(patch_size)
        self.img_size = img_size
        self.layer = layer
        if layer==0:
            self.proj1 = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride,
                                  padding=(patch_size[0] // 2, patch_size[1] // 2))
            self.proj2 = nn.Conv2d(embed_dim, embed_dim//4, kernel_size=3, stride=1,
                                  padding=(3//2, 3//2))
            self.norm1 = nn.BatchNorm2d(embed_dim)
            self.norm2 = nn.BatchNorm2d(embed_dim//4)
            self.act = nn.GELU()
            self.c, self.h, self.w = 16, 2, 2

            self.H, self.W = self.img_size//(self.h*stride), self.img_size//(self.w*stride)
            self.pos_embed1 = nn.Parameter(torch.zeros(1, embed_dim, self.h, self.w))
            trunc_normal_(self.pos_embed1, std=.02)
        else:
            #[64, 128, 320, 512]
            '''self.proj1 = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride,
                                  padding=(patch_size[0] // 2, patch_size[1] // 2))
            self.norm1 = nn.BatchNorm2d(embed_dim)'''

            self.c, self.cout, self.h, self.w, self.ks = {1:(16,8,2,2,5), 2:(8,5,4,4,7), 3:(5,2,8,8,9)}[layer]
            self.proj2 = nn.Conv2d(self.c, self.cout, kernel_size=self.ks, padding=self.ks//2)
            self.norm2 = nn.BatchNorm2d(self.cout)

            self.H, self.W = self.img_size//stride, self.img_size//stride
            pos_embed = nn.Parameter(torch.zeros(1, self.cout, self.h*2, self.w*2))
            trunc_normal_(pos_embed, std=.02)
            setattr(self, f"pos_embed{layer+1}", pos_embed)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x):
        if self.layer == 0:
            x = self.act(self.norm1(self.proj1(x) + self.pos_embed1.repeat(1,1,self.H,self.W)))
            x = self.norm2(self.proj2(x))
            x = rearrange(x, 'b c (n1 p1) (n2 p2) -> b (c p1 p2) n1 n2', p1=self.h, p2=self.w)
        else:
            #x1 = self.norm1(self.proj1(x))
            x2 = rearrange(x, 'b (c p1 p2) n1 n2 -> b c (n1 p1) (n2 p2)', p1=self.h, p2=self.w)
            x2 = self.norm2(self.proj2(x2) + getattr(self, f"pos_embed{self.layer+1}").repeat(1,1,self.H,self.W))
            x2 = rearrange(x2, 'b c (n1 p1) (n2 p2) -> b (c p1 p2) n1 n2', p1=self.h*2, p2=self.w*2)
            x = x2 #0.5 * (x1 + x2)

        _, _, H, W = x.shape
        return x, H, W


class VAN(nn.Module):
    def __init__(self, img_size=224, in_chans=3, num_classes=1000, embed_dims=[64, 128, 256, 512],
                mlp_ratios=[4, 4, 4, 4], drop_rate=0., drop_path_rate=0., norm_layer=nn.LayerNorm,
                 depths=[3, 4, 6, 3], num_stages=4, flag=False):
        super().__init__()
        if flag == False:
            self.num_classes = num_classes
        self.depths = depths
        self.num_stages = num_stages

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule
        cur = 0

        for i in range(num_stages):
            patch_embed = OverlapPatchEmbed(img_size=img_size if i == 0 else img_size // (2 ** (i + 1)),
                                            patch_size=5 if i == 0 else 3,
                                            stride=2 if i == 0 else 2,
                                            in_chans=in_chans if i == 0 else embed_dims[i - 1]//1,
                                            embed_dim=embed_dims[i],
                                            layer=i)

            block = nn.ModuleList([Block(
                dim=embed_dims[i], mlp_ratio=mlp_ratios[i], drop=drop_rate, drop_path=dpr[cur + j], layer=i)
                for j in range(depths[i])])
            norm = norm_layer(embed_dims[i])
            cur += depths[i]

            setattr(self, f"patch_embed{i + 1}", patch_embed)
            setattr(self, f"block{i + 1}", block)
            setattr(self, f"norm{i + 1}", norm)

        # classification head
        self.head = nn.Linear(embed_dims[3], num_classes) if num_classes > 0 else nn.Identity()

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def freeze_patch_emb(self):
        self.patch_embed1.requires_grad = False

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed1', 'pos_embed2', 'pos_embed3', 'pos_embed4', 'cls_token'}  # has pos_embed may be better

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, x):
        B = x.shape[0]

        for i in range(self.num_stages):
            patch_embed = getattr(self, f"patch_embed{i + 1}")
            block = getattr(self, f"block{i + 1}")
            norm = getattr(self, f"norm{i + 1}")
            x, H, W = patch_embed(x)
            for blk in block:
                x = blk(x)
            x = x.flatten(2).transpose(1, 2)
            x = norm(x)
            if i != self.num_stages - 1:
                x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()

        return x.mean(dim=1)

    def forward(self, x):
        x = self.forward_features(x)
        x = self.head(x)

        return x


model_urls = {
    "van_tiny": "https://huggingface.co/Visual-Attention-Network/VAN-Tiny-original/resolve/main/van_tiny_754.pth.tar",
    "van_small": "https://huggingface.co/Visual-Attention-Network/VAN-Small-original/resolve/main/van_small_811.pth.tar",
    "van_base": "https://huggingface.co/Visual-Attention-Network/VAN-Base-original/resolve/main/van_base_828.pth.tar",
    "van_large": "https://huggingface.co/Visual-Attention-Network/VAN-Large-original/resolve/main/van_large_839.pth.tar",
}


def load_model_weights(model, arch, kwargs):
    url = model_urls[arch]
    checkpoint = torch.hub.load_state_dict_from_url(
        url=url, map_location="cpu", check_hash=True
    )
    strict = True
    if "num_classes" in kwargs and kwargs["num_classes"] != 1000:
        strict = False
        del checkpoint["state_dict"]["head.weight"]
        del checkpoint["state_dict"]["head.bias"]
    model.load_state_dict(checkpoint["state_dict"], strict=strict)
    return model


@register_model
def van_tiny(pretrained=False, **kwargs):
    model = VAN(
        embed_dims=[32, 64, 160, 256], mlp_ratios=[8, 8, 4, 4],
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 3, 5, 2],
        **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        model = load_model_weights(model, "van_tiny", kwargs)
    return model


@register_model
def van_small(pretrained=False, **kwargs):
    model = VAN(
        embed_dims=[64, 128, 320, 512], mlp_ratios=[8, 8, 4, 4],
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[2, 2, 4, 2],
        **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        model = load_model_weights(model, "van_small", kwargs)
    return model

@register_model
def van_base(pretrained=False, **kwargs):
    model = VAN(
        embed_dims=[64, 128, 320, 512], mlp_ratios=[8, 8, 4, 4],
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 3, 12, 3],
        **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        model = load_model_weights(model, "van_base", kwargs)
    return model

@register_model
def van_large(pretrained=False, **kwargs):
    model = VAN(
        embed_dims=[64, 128, 320, 512], mlp_ratios=[8, 8, 4, 4],
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 5, 27, 3],
        **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        model = load_model_weights(model, "van_large", kwargs)
    return model