"""图片下载 → 裁剪 → 生成「裁出图 + 挖空后整图」→ 上传 OSS"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import aiohttp
import oss2
from dotenv import load_dotenv
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field, model_validator

load_dotenv()


class OSSConfig(BaseModel):
    access_key_id: str = Field(default_factory=lambda: os.getenv("OSS_ACCESS_KEY_ID", ""))
    access_key_secret: str = Field(default_factory=lambda: os.getenv("OSS_ACCESS_KEY_SECRET", ""))
    bucket_name: str = Field(default_factory=lambda: os.getenv("OSS_BUCKET_NAME", ""))
    endpoint: str = Field(default_factory=lambda: os.getenv("OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com"))
    upload_path_prefix: str = Field(default_factory=lambda: os.getenv("OSS_UPLOAD_PATH_PREFIX", "crop"))

    def public_url(self, object_key: str) -> str:
        endpoint = self.endpoint.removeprefix("https://").removeprefix("http://")
        return f"https://{self.bucket_name}.{endpoint}/{object_key.lstrip('/')}"


class CropBBox(BaseModel):
    """像素坐标：左上角 + 宽高"""

    image_url: str = Field(..., description="原图 URL")
    x: int = Field(..., ge=0, description="裁剪区左上角 x")
    y: int = Field(..., ge=0, description="裁剪区左上角 y")
    width: int = Field(..., gt=0, description="裁剪宽度")
    height: int = Field(..., gt=0, description="裁剪高度")
    output_format: Literal["png", "jpeg", "webp"] | None = Field(
        default=None,
        description="cropped 输出格式；remaining 固定为 PNG 透明底，便于拼回",
    )


class ResizeImage(BaseModel):
    """缩放图片到指定尺寸并上传 OSS"""

    image_url: str = Field(..., description="原图 URL")
    width: int = Field(..., gt=0, description="目标宽度（像素）")
    height: int = Field(..., gt=0, description="目标高度（像素）")
    output_format: Literal["png", "jpeg", "webp"] | None = Field(
        default=None,
        description="输出格式；默认跟随原图",
    )


class ResizeResult(BaseModel):
    url: str
    object_key: str
    width: int
    height: int
    source_width: int
    source_height: int


class ImageSizeResult(BaseModel):
    """图片像素尺寸"""

    width: int = Field(description="宽度（px）")
    height: int = Field(description="高度（px）")


class UploadFileRequest(BaseModel):
    """将文件内容落盘并上传 OSS"""

    content: str = Field(..., description="文件内容（base64）")
    ext: str = Field(..., description="文件后缀，如 png、jpg、pdf")
    filename: str | None = Field(
        default=None,
        description="文件名（不含后缀）；缺省使用时间戳",
    )


class UploadFileResult(BaseModel):
    """OSS 上传结果"""

    url: str = Field(description="OSS 公网访问地址")
    object_key: str = Field(description="OSS 对象路径")
    local_path: str = Field(description="本地落盘路径")


class CropXYXY(BaseModel):
    """像素坐标：左上、右下两点"""

    image_url: str
    x1: int = Field(..., ge=0)
    y1: int = Field(..., ge=0)
    x2: int = Field(..., ge=0)
    y2: int = Field(..., ge=0)
    output_format: Literal["png", "jpeg", "webp"] | None = None

    @model_validator(mode="after")
    def check_box(self) -> "CropXYXY":
        if self.x1 == self.x2 or self.y1 == self.y2:
            raise ValueError("x1/x2 或 y1/y2 不能相同")
        return self


class ImagePart(BaseModel):
    url: str
    object_key: str
    width: int
    height: int


class PasteBackHint(BaseModel):
    """拼回整图时的定位信息（与 crop_box 一致）"""

    offset_x: int = Field(description="贴回时的左上角 x")
    offset_y: int = Field(description="贴回时的左上角 y")
    canvas_width: int
    canvas_height: int
    note: str = (
        "流程：编辑 cropped → 将处理结果贴到 remaining 的 (offset_x, offset_y)。"
        "remaining 为 PNG 透明底，挖空处 alpha=0，便于叠图拼回。"
    )


class CropPairResult(BaseModel):
    """指定裁剪范围 + 两张结果图（支持裁下→处理→拼回）"""

    crop_box: dict[str, int] = Field(
        description="实际裁剪范围（像素，已 clamp 到原图内）",
    )
    cropped: ImagePart = Field(description="裁下来的区域图（拿去单独处理）")
    remaining: ImagePart = Field(
        description="拼回用底图：原图尺寸 PNG，指定区域透明挖空，保留其余部分",
    )
    paste_back: PasteBackHint = Field(description="拼回定位信息")

class AddBorderRequest(BaseModel):
    image_url: str = Field(..., description="原图 URL")
    position: Literal["inside", "outside"] = Field(
        default="outside",
        description="inside=不扩画布；outside=四周各扩 width px",
    )
    color: str = Field(..., description="边框颜色，如 #FF0000 或 rgb(255,0,0)")
    width: int = Field(..., gt=0, description="边框宽度（像素）")
    output_format: Literal["png", "jpeg", "webp"] | None = Field(
        default=None,
        description="输出格式；默认跟随原图",
    )

def _clamp_box(left: int, top: int, right: int, bottom: int, img_w: int, img_h: int) -> tuple[int, int, int, int]:
    left = max(0, min(left, img_w))
    top = max(0, min(top, img_h))
    right = max(0, min(right, img_w))
    bottom = max(0, min(bottom, img_h))
    if right <= left or bottom <= top:
        raise ValueError(f"invalid crop box ({left},{top},{right},{bottom}) on {img_w}x{img_h}")
    return left, top, right, bottom


def _resolve_format(img: Image.Image, output_format: str | None) -> str:
    fmt = (output_format or (img.format or "PNG")).upper()
    if fmt == "JPG":
        fmt = "JPEG"
    if fmt not in {"PNG", "JPEG", "WEBP"}:
        fmt = "PNG"
    return fmt


async def download_image(url: str, timeout: int = 120) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("image_url 必须是 http/https")
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            resp.raise_for_status()
            return await resp.read()


def _to_bytes(img: Image.Image, fmt: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    save_img = img
    if fmt == "JPEG":
        if img.mode in ("RGBA", "LA", "P"):
            save_img = img.convert("RGB")
        save_img.save(buf, format="JPEG", quality=95)
    elif fmt == "PNG":
        if save_img.mode not in ("RGBA", "LA"):
            save_img = save_img.convert("RGBA")
        save_img.save(buf, format="PNG", optimize=True)
    elif fmt == "WEBP":
        if save_img.mode not in ("RGBA", "LA"):
            save_img = save_img.convert("RGBA")
        save_img.save(buf, format="WEBP", lossless=True)
    else:
        save_img.save(buf, format=fmt)
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(fmt, "png")
    return buf.getvalue(), ext


def upload_to_oss(
    file_bytes: bytes,
    ext: str,
    cfg: OSSConfig,
    tag: str = "",
    filename: str | None = None,
) -> tuple[str, str]:
    if not all([cfg.access_key_id, cfg.access_key_secret, cfg.bucket_name, cfg.endpoint]):
        raise ValueError("OSS 环境变量未配置完整")
    month = datetime.now().strftime("%Y/%m")
    clean_ext = ext.lstrip(".").lower()
    if filename:
        stem = Path(filename).stem or filename
        object_name = f"{stem}.{clean_ext}" if clean_ext else stem
    else:
        digest = hashlib.md5(file_bytes).hexdigest()
        suffix = f"_{tag}" if tag else ""
        object_name = f"{digest}{suffix}.{clean_ext}"
    object_key = f"{cfg.upload_path_prefix.rstrip('/')}/{month}/{object_name}"
    endpoint = cfg.endpoint if cfg.endpoint.startswith("http") else f"https://{cfg.endpoint}"
    bucket = oss2.Bucket(oss2.Auth(cfg.access_key_id, cfg.access_key_secret), endpoint, cfg.bucket_name)
    bucket.put_object(object_key, file_bytes)
    return cfg.public_url(object_key), object_key


def _decode_file_content(content: bytes | str) -> bytes:
    if isinstance(content, bytes):
        return content
    text = content.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=True)
    except Exception:
        return content.encode("utf-8")


def _resolve_upload_filename(filename: str | None, ext: str) -> str:
    clean_ext = ext.lstrip(".").lower()
    if filename and filename.strip():
        stem = Path(filename.strip()).stem or filename.strip()
    else:
        stem = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{stem}.{clean_ext}" if clean_ext else stem


def _make_remaining(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """拼回底图：原图尺寸 PNG，box 区域透明挖空（alpha=0）"""
    left, top, right, bottom = box
    remaining = img.convert("RGBA")
    draw = ImageDraw.Draw(remaining)
    draw.rectangle([left, top, right, bottom], fill=(0, 0, 0, 0))
    return remaining


def _upload_part(img: Image.Image, fmt: str, cfg: OSSConfig, tag: str) -> ImagePart:
    data, ext = _to_bytes(img, fmt)
    url, key = upload_to_oss(data, ext, cfg, tag=tag)
    w, h = img.size
    return ImagePart(url=url, object_key=key, width=w, height=h)


async def crop_by_bbox(req: CropBBox, cfg: OSSConfig | None = None) -> CropPairResult:
    cfg = cfg or OSSConfig()
    raw = await download_image(req.image_url)
    img = Image.open(io.BytesIO(raw))
    w, h = img.size

    box = _clamp_box(req.x, req.y, req.x + req.width, req.y + req.height, w, h)
    left, top, right, bottom = box
    cropped = img.crop(box)
    remaining = _make_remaining(img, box)

    cropped_fmt = _resolve_format(img, req.output_format)
    cropped_part = _upload_part(cropped, cropped_fmt, cfg, tag="crop")
    # remaining 固定 PNG 透明底，便于「裁下→处理→贴回」
    remaining_part = _upload_part(remaining, "PNG", cfg, tag="remaining")

    return CropPairResult(
        crop_box={
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
        },
        cropped=cropped_part,
        remaining=remaining_part,
        paste_back=PasteBackHint(
            offset_x=left,
            offset_y=top,
            canvas_width=w,
            canvas_height=h,
        ),
    )

def _parse_border_color(color: str) -> tuple[int, int, int, int]:
    """解析颜色为 RGBA"""
    from PIL import ImageColor
    rgba = ImageColor.getcolor(color, "RGBA")
    if len(rgba) == 3:
        return (*rgba, 255)
    return rgba
def _add_border(
    img: Image.Image,
    position: Literal["inside", "outside"],
    color: str,
    width: int,
) -> Image.Image:
    rgba = img.convert("RGBA")
    fill = _parse_border_color(color)
    if position == "outside":
        w, h = rgba.size
        canvas = Image.new("RGBA", (w + 2 * width, h + 2 * width), fill)
        canvas.paste(rgba, (width, width), rgba)
        return canvas
    # inside：四边各画 width 厚的矩形
    out = rgba.copy()
    draw = ImageDraw.Draw(out)
    w, h = out.size
    for i in range(width):
        draw.rectangle([i, i, w - 1 - i, h - 1 - i], outline=fill)
    return out


async def crop_by_xyxy(req: CropXYXY, cfg: OSSConfig | None = None) -> CropPairResult:
    left, top = min(req.x1, req.x2), min(req.y1, req.y2)
    right, bottom = max(req.x1, req.x2), max(req.y1, req.y2)
    return await crop_by_bbox(
        CropBBox(
            image_url=req.image_url,
            x=left,
            y=top,
            width=right - left,
            height=bottom - top,
            output_format=req.output_format,
        ),
        cfg=cfg,
    )


async def get_image_size(image_url: str) -> ImageSizeResult:
    """下载图片并返回宽高（px）"""
    raw = await download_image(image_url)
    with Image.open(io.BytesIO(raw)) as img:
        width, height = img.size
    return ImageSizeResult(width=width, height=height)


async def upload_file_content(
    content: bytes | str,
    ext: str,
    filename: str | None = None,
    cfg: OSSConfig | None = None,
) -> UploadFileResult:
    """将文件内容写入本地，再上传 OSS，返回 OSS 路径"""
    if not ext or not ext.strip().lstrip("."):
        raise ValueError("ext 不能为空")
    cfg = cfg or OSSConfig()
    file_bytes = _decode_file_content(content)
    local_name = _resolve_upload_filename(filename, ext)
    local_dir = Path(os.getenv("UPLOAD_LOCAL_DIR", tempfile.gettempdir())) / "crop_uploads"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / local_name
    local_path.write_bytes(file_bytes)

    url, object_key = upload_to_oss(
        file_bytes,
        ext=ext,
        cfg=cfg,
        filename=local_name,
    )
    return UploadFileResult(
        url=url,
        object_key=object_key,
        local_path=str(local_path),
    )


async def upload_file(req: UploadFileRequest, cfg: OSSConfig | None = None) -> UploadFileResult:
    return await upload_file_content(
        content=req.content,
        ext=req.ext,
        filename=req.filename,
        cfg=cfg,
    )


async def resize_image(req: ResizeImage, cfg: OSSConfig | None = None) -> ResizeResult:
    """下载图片，缩放到指定宽高后上传 OSS"""
    cfg = cfg or OSSConfig()
    raw = await download_image(req.image_url)
    img = Image.open(io.BytesIO(raw))
    src_w, src_h = img.size
    if img.mode not in ("RGBA", "LA"):
        img = img.convert("RGBA")
    resized = img.resize((req.width, req.height), Image.Resampling.LANCZOS)
    fmt = _resolve_format(img, req.output_format)
    part = _upload_part(resized, fmt, cfg, tag="resize")
    return ResizeResult(
        url=part.url,
        object_key=part.object_key,
        width=part.width,
        height=part.height,
        source_width=src_w,
        source_height=src_h,
    )


async def compose_image_back(
    remaining_url: str,
    patch_url: str,
    offset_x: int,
    offset_y: int,
    output_format: Literal["png", "jpeg", "webp"] | None = None,
    cfg: OSSConfig | None = None,
) -> ImagePart:
    """将处理后的 cropped 贴回 remaining 底图，生成完整图"""
    cfg = cfg or OSSConfig()
    remaining_raw = await download_image(remaining_url)
    patch_raw = await download_image(patch_url)
    remaining = Image.open(io.BytesIO(remaining_raw)).convert("RGBA")
    patch = Image.open(io.BytesIO(patch_raw)).convert("RGBA")
    canvas = remaining.copy()
    canvas.paste(patch, (offset_x, offset_y), patch)
    fmt = output_format.upper() if output_format else "PNG"
    if fmt not in ("JPEG", "PNG", "WEBP"):
        fmt = "PNG"
    return _upload_part(canvas, fmt, cfg, tag="composed")


async def add_border(
    req: AddBorderRequest,
    cfg: OSSConfig | None = None,
) -> ImagePart:
    cfg = cfg or OSSConfig()
    raw = await download_image(req.image_url)
    img = Image.open(io.BytesIO(raw))
    bordered = _add_border(img, req.position, req.color, req.width)
    fmt = _resolve_format(img, req.output_format)
    return _upload_part(bordered, fmt, cfg, tag="border")