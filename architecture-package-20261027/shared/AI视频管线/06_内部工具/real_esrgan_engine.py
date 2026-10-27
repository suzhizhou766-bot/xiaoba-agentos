"""不依赖 BasicSR 的 Real-ESRGAN x4 推理器（Apple Silicon / PyTorch）。"""

from pathlib import Path

import numpy as np
from PIL import Image
import torch
from torch import nn
from torch.nn import functional as F


class ResidualDenseBlock(nn.Module):
    def __init__(self, channels=64, growth=32):
        super().__init__()
        self.conv1 = nn.Conv2d(channels, growth, 3, 1, 1)
        self.conv2 = nn.Conv2d(channels + growth, growth, 3, 1, 1)
        self.conv3 = nn.Conv2d(channels + growth * 2, growth, 3, 1, 1)
        self.conv4 = nn.Conv2d(channels + growth * 3, growth, 3, 1, 1)
        self.conv5 = nn.Conv2d(channels + growth * 4, channels, 3, 1, 1)
        self.act = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        x1 = self.act(self.conv1(x))
        x2 = self.act(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.act(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.act(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x


class RRDB(nn.Module):
    def __init__(self, channels=64, growth=32):
        super().__init__()
        self.rdb1 = ResidualDenseBlock(channels, growth)
        self.rdb2 = ResidualDenseBlock(channels, growth)
        self.rdb3 = ResidualDenseBlock(channels, growth)

    def forward(self, x):
        out = self.rdb3(self.rdb2(self.rdb1(x)))
        return out * 0.2 + x


class RRDBNet(nn.Module):
    def __init__(self, blocks=23, channels=64, growth=32):
        super().__init__()
        self.conv_first = nn.Conv2d(3, channels, 3, 1, 1)
        self.body = nn.Sequential(*[RRDB(channels, growth) for _ in range(blocks)])
        self.conv_body = nn.Conv2d(channels, channels, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(channels, channels, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(channels, channels, 3, 1, 1)
        self.conv_hr = nn.Conv2d(channels, channels, 3, 1, 1)
        self.conv_last = nn.Conv2d(channels, 3, 3, 1, 1)
        self.act = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        feat = self.conv_first(x)
        feat = self.conv_body(self.body(feat)) + feat
        feat = self.act(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
        feat = self.act(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
        return self.conv_last(self.act(self.conv_hr(feat)))


class SRVGGNetCompact(nn.Module):
    """Official realesr-general-x4v3 architecture."""
    def __init__(self, num_feat=64, num_conv=32, upscale=4):
        super().__init__()
        body = [nn.Conv2d(3, num_feat, 3, 1, 1), nn.PReLU(num_parameters=num_feat)]
        for _ in range(num_conv):
            body += [nn.Conv2d(num_feat, num_feat, 3, 1, 1), nn.PReLU(num_parameters=num_feat)]
        body += [nn.Conv2d(num_feat, 3 * upscale * upscale, 3, 1, 1)]
        self.body = nn.Sequential(*body)
        self.upsampler = nn.PixelShuffle(upscale)
        self.upscale = upscale

    def forward(self, x):
        return self.upsampler(self.body(x)) + F.interpolate(x, scale_factor=self.upscale, mode="nearest")


class RealESRGAN:
    scale = 4

    def __init__(self, model_path, tile=256, tile_pad=12):
        self.device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        self.tile, self.tile_pad = tile, tile_pad
        checkpoint = torch.load(Path(model_path), map_location="cpu", weights_only=True)
        state = checkpoint.get("params_ema", checkpoint.get("params", checkpoint))
        self.model = SRVGGNetCompact()
        self.model.load_state_dict(state, strict=True)
        self.model.eval().to(self.device)

    @torch.inference_mode()
    def upscale(self, image):
        rgba = image.mode == "RGBA"
        alpha = image.getchannel("A") if rgba else None
        rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        # Official Real-ESRGAN weights are trained with OpenCV BGR arrays.
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        tensor = torch.from_numpy(bgr.transpose(2, 0, 1)).unsqueeze(0)
        height, width = tensor.shape[-2:]
        output = np.empty((height * self.scale, width * self.scale, 3), dtype=np.uint8)
        for y in range(0, height, self.tile):
            for x in range(0, width, self.tile):
                y0, x0 = max(y - self.tile_pad, 0), max(x - self.tile_pad, 0)
                y1, x1 = min(y + self.tile, height), min(x + self.tile, width)
                y2, x2 = min(y1 + self.tile_pad, height), min(x1 + self.tile_pad, width)
                tile = tensor[:, :, y0:y2, x0:x2].to(self.device)
                predicted = self.model(tile).clamp_(0, 1)[0].detach().cpu().numpy()
                top, left = (y - y0) * self.scale, (x - x0) * self.scale
                bottom, right = top + (y1 - y) * self.scale, left + (x1 - x) * self.scale
                patch = (predicted[:, top:bottom, left:right].transpose(1, 2, 0) * 255.0).round().astype(np.uint8)
                output[y * self.scale:y1 * self.scale, x * self.scale:x1 * self.scale] = patch
        restored = Image.fromarray(output[:, :, ::-1], "RGB")
        if alpha is not None:
            restored.putalpha(alpha.resize(restored.size, Image.Resampling.LANCZOS))
        return restored
