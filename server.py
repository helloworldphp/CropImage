"""
Image Crop MCP Server (ModelScope Hosted)

默认 STDIO，供魔搭 MCP 广场 / 函数计算托管（平台再转 SSE）。
本地调试也可切 SSE / HTTP：
  MCP_TRANSPORT=stdio python server.py
  MCP_TRANSPORT=sse   python server.py   -> http://host:8091/sse
  MCP_TRANSPORT=http  python server.py   -> http://host:8091/mcp
"""

from __future__ import annotations

import os
from typing import Any, Literal

from dotenv import load_dotenv
from fastmcp import FastMCP

from crop_service import (
    AddBorderRequest,
    CropBBox,
    CropPairResult,
    CropXYXY,
    ResizeImage,
    UploadFileRequest,
    add_border,
    compose_image_back,
    crop_by_bbox,
    crop_by_xyxy,
    get_image_size,
    resize_image,
    upload_file,
)

load_dotenv()

mcp = FastMCP(
    name="image-crop-mcp",
    instructions=(
        "提供图片裁剪并上传 OSS 的能力。"
        "image_url 必须是公网可访问的 http/https 地址。"
        "每次裁剪返回：crop_box（实际范围）、cropped（裁出的区域图 URL）、"
        "remaining（原图尺寸 PNG 透明底，挖空区域便于拼回）、paste_back（贴回坐标）。"
        "流程：crop → 处理 cropped → resize_image 对齐尺寸 → compose_image_back 贴回 remaining。"
        "crop_image_by_bbox: image_url + x,y,width,height（像素）。"
        "crop_image_by_xyxy: image_url + x1,y1,x2,y2（像素）。"
        "resize_image: image_url + width,height，缩放后上传 OSS。"
        "compose_image_back: remaining_url + patch_url + offset_x,offset_y。"
    ),
)


def _to_response(result: CropPairResult) -> dict[str, Any]:
    data = result.model_dump()
    data["crop_url"] = result.cropped.url
    data["remaining_url"] = result.remaining.url
    return data


@mcp.tool(
    name="crop_image_by_bbox",
    description="按像素 bbox 裁剪：返回裁出区域图 + 挖空指定区域后的整图 URL",
)
async def crop_image_by_bbox(
    image_url: str,
    x: int,
    y: int,
    width: int,
    height: int,
    output_format: Literal["png", "jpeg", "webp"] | None = None,
) -> dict[str, Any]:
    """
    Args:
        image_url: 原图地址（公网 http/https）
        x, y: 左上角坐标（像素）
        width, height: 裁剪宽高（像素）
        output_format: 可选 png/jpeg/webp
    """
    req = CropBBox(
        image_url=image_url,
        x=x,
        y=y,
        width=width,
        height=height,
        output_format=output_format,
    )
    return _to_response(await crop_by_bbox(req))


@mcp.tool(
    name="crop_image_by_xyxy",
    description="按左上/右下两点裁剪：返回裁出区域图 + 挖空后的整图 URL",
)
async def crop_image_by_xyxy(
    image_url: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    output_format: Literal["png", "jpeg", "webp"] | None = None,
) -> dict[str, Any]:
    req = CropXYXY(
        image_url=image_url,
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        output_format=output_format,
    )
    return _to_response(await crop_by_xyxy(req))


@mcp.tool(
    name="crop_image_batch",
    description="批量裁剪：每项返回 crop_box、cropped、remaining 两张图地址",
)
async def crop_image_batch(
    image_url: str,
    boxes: list[dict[str, int]],
    output_format: Literal["png", "jpeg", "webp"] | None = None,
) -> dict[str, Any]:
    """
    Args:
        image_url: 原图（公网）
        boxes: [{"x":0,"y":0,"width":100,"height":80}, ...]
    """
    items: list[dict[str, Any]] = []
    for box in boxes:
        req = CropBBox(
            image_url=image_url,
            x=int(box["x"]),
            y=int(box["y"]),
            width=int(box["width"]),
            height=int(box["height"]),
            output_format=output_format,
        )
        items.append(_to_response(await crop_by_bbox(req)))
    return {
        "items": items,
        "count": len(items),
        "crop_urls": [i["crop_url"] for i in items],
        "remaining_urls": [i["remaining_url"] for i in items],
    }


@mcp.tool(
    name="get_image_size",
    description="根据图片 URL 返回宽高（像素 px）",
)
async def get_image_size_tool(image_url: str) -> dict[str, Any]:
    """
    Args:
        image_url: 图片地址（公网）
    """
    return (await get_image_size(image_url)).model_dump()


@mcp.tool(
    name="upload_file",
    description="将文件内容写入本地并上传 OSS；filename 缺省用时间戳",
)
async def upload_file_tool(
    content: str,
    ext: str,
    filename: str | None = None,
) -> dict[str, Any]:
    """
    Args:
        content: 文件内容（base64）
        ext: 文件后缀，如 png、jpg、pdf
        filename: 文件名（不含后缀）；缺省使用时间戳
    """
    result = await upload_file(
        UploadFileRequest(content=content, ext=ext, filename=filename),
    )
    return result.model_dump()


@mcp.tool(
    name="resize_image",
    description="将图片缩放到指定宽高并上传 OSS，常用于 AI 出图后对齐 crop_box 尺寸再拼回",
)
async def resize_image_tool(
    image_url: str,
    width: int,
    height: int,
    output_format: Literal["png", "jpeg", "webp"] | None = None,
) -> dict[str, Any]:
    """
    Args:
        image_url: 待缩放图片 URL（公网）
        width, height: 目标宽高（像素），如 crop_box.width / crop_box.height
        output_format: 可选 png/jpeg/webp
    """
    req = ResizeImage(
        image_url=image_url,
        width=width,
        height=height,
        output_format=output_format,
    )
    part = await resize_image(req)
    return part.model_dump()


@mcp.tool(
    name="compose_image_back",
    description="将处理后的 cropped 贴回 remaining 底图，生成完整图 URL",
)
async def compose_image_back_tool(
    remaining_url: str,
    patch_url: str,
    offset_x: int,
    offset_y: int,
    output_format: Literal["png", "jpeg", "webp"] | None = None,
) -> dict[str, Any]:
    """
    Args:
        remaining_url: 裁剪时返回的 remaining_url（PNG 透明底）
        patch_url: 处理后的 cropped 图 URL
        offset_x, offset_y: paste_back 中的贴回坐标（即 crop_box.x / crop_box.y）
        output_format: 合成图格式，默认 png
    """
    part = await compose_image_back(
        remaining_url=remaining_url,
        patch_url=patch_url,
        offset_x=offset_x,
        offset_y=offset_y,
        output_format=output_format,
    )
    return {
        "url": part.url,
        "object_key": part.object_key,
        "width": part.width,
        "height": part.height,
    }


@mcp.tool(
    name="add_border",
    description="给图片加边框并上传 OSS；outside 扩画布，inside 不扩画布",
)
async def add_border_tool(
    image_url: str,
    position: Literal["inside", "outside"],
    color: str,
    width: int,
    output_format: Literal["png", "jpeg", "webp"] | None = None,
) -> dict[str, Any]:
    """
    Args:
        image_url: 原图 URL（公网）
        position: inside=边框画在图内；outside=四周各扩 width px
        color: 边框颜色，如 #FF0000、#000000、rgb(255,0,0)
        width: 边框宽度（像素），> 0
        output_format: 可选 png/jpeg/webp，默认跟随原图
    """
    req = AddBorderRequest(
        image_url=image_url,
        position=position,
        color=color,
        width=width,
        output_format=output_format,
    )
    part = await add_border(req)
    return {
        "url": part.url,
        "object_key": part.object_key,
        "width": part.width,
        "height": part.height,
        "position": position,
        "border_width": width,
        "color": color,
    }


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport in ("sse", "http", "streamable_http", "streamable-http"):
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8091"))
        if transport == "sse":
            mcp.run(transport="sse", host=host, port=port)
        else:
            mcp.run(transport="http", host=host, port=port)
    else:
        # ModelScope Hosted / 本地 Inspector 默认走 STDIO
        mcp.run(transport="stdio")
