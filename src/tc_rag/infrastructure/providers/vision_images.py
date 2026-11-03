"""保留总览与原像素局部图；不恢复整页扫描或 OCR 路线。"""

import io
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageOps

from tc_rag.application.ports.errors import VisionReadError
from tc_rag.core.config import VisionProcessing


@dataclass(frozen=True)
class ImageView:
    view_id: str
    bounds: tuple[int, int, int, int]
    size: tuple[int, int]
    png: bytes

    def metadata(self) -> dict[str, Any]:
        return {"view_id": self.view_id, "bounds": list(self.bounds), "size": list(self.size)}


def _starts(length: int, edge: int, overlap: int) -> list[int]:
    if length <= edge:
        return [0]
    positions = list(range(0, length - edge + 1, edge - overlap))
    if positions[-1] != length - edge:
        positions.append(length - edge)
    return positions


def _view(image: Image.Image, name: str, bounds: tuple[int, int, int, int]) -> ImageView:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return ImageView(name, bounds, image.size, buffer.getvalue())


def prepare_views(image: bytes, options: VisionProcessing) -> list[ImageView]:
    """总览用于理解全局连线；大图局部保持原像素，不锐化或生成不存在的细节。"""
    with Image.open(io.BytesIO(image)) as source:
        oriented = ImageOps.exif_transpose(source)
        rgba = oriented.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        full = background.convert("RGB")
    width, height = full.size
    overview = full.copy()
    overview.thumbnail((options.max_edge, options.max_edge), Image.Resampling.LANCZOS)
    result = [_view(overview, "overview", (0, 0, width, height))]
    if max(width, height) <= options.max_edge:
        return result
    xs = _starts(width, options.tile_edge, options.tile_overlap)
    ys = _starts(height, options.tile_edge, options.tile_overlap)
    # 禁止默默丢掉后半张长图，或在超过上限时悄悄只发送模糊总览。
    if len(xs) * len(ys) > options.max_tiles:
        raise VisionReadError("IMAGE_VIEW_BUDGET")
    for y in ys:
        for x in xs:
            bounds = (x, y, min(x + options.tile_edge, width), min(y + options.tile_edge, height))
            result.append(_view(full.crop(bounds), f"tile_{len(result):03}", bounds))
    return result
