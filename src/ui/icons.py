"""Vector icon painter.

Icons are drawn with QPainterPath instead of shipping SVG/PNG assets: they stay
crisp at every DPI, inherit the palette, and add zero bytes to the installer.
"""

from __future__ import annotations


from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

ICON_NAMES = (
    "play",
    "pause",
    "stop",
    "previous",
    "next",
    "repeat",
    "repeat_one",
    "shuffle",
    "folder",
    "search",
    "settings",
    "queue",
    "library",
    "chip",
    "refresh",
    "close",
    "check",
    "power",
    "volume_off",
    "info",
    "eject",
    "save",
    "open",
)


def _play_path(rect: QRectF) -> QPainterPath:
    path = QPainterPath()
    left = rect.left() + rect.width() * 0.26
    top = rect.top() + rect.height() * 0.16
    height = rect.height() * 0.68
    width = rect.width() * 0.56
    path.moveTo(left, top)
    path.lineTo(left + width, top + height / 2)
    path.lineTo(left, top + height)
    path.closeSubpath()
    return path


def _pause_paths(rect: QRectF) -> list[QPainterPath]:
    width = rect.width() * 0.18
    height = rect.height() * 0.62
    top = rect.top() + rect.height() * 0.19
    gap = rect.width() * 0.16
    left_x = rect.center().x() - gap / 2 - width
    right_x = rect.center().x() + gap / 2
    paths = []
    for x in (left_x, right_x):
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, top, width, height), 1.8, 1.8)
        paths.append(path)
    return paths


def _skip_path(rect: QRectF, forward: bool) -> list[QPainterPath]:
    width = rect.width() * 0.40
    height = rect.height() * 0.60
    top = rect.top() + rect.height() * 0.20
    bar_width = rect.width() * 0.11
    paths: list[QPainterPath] = []
    triangle = QPainterPath()
    if forward:
        base_x = rect.left() + rect.width() * 0.18
        triangle.moveTo(base_x, top)
        triangle.lineTo(base_x + width, top + height / 2)
        triangle.lineTo(base_x, top + height)
        bar = QPainterPath()
        bar.addRoundedRect(QRectF(base_x + width + rect.width() * 0.06, top, bar_width, height), 1.4, 1.4)
    else:
        base_x = rect.left() + rect.width() * 0.36
        triangle.moveTo(base_x + width, top)
        triangle.lineTo(base_x, top + height / 2)
        triangle.lineTo(base_x + width, top + height)
        bar = QPainterPath()
        bar.addRoundedRect(QRectF(base_x - rect.width() * 0.06 - bar_width, top, bar_width, height), 1.4, 1.4)
    triangle.closeSubpath()
    paths.append(triangle)
    paths.append(bar)
    return paths


def _repeat_paths(rect: QRectF, one: bool) -> list[QPainterPath]:
    margin = rect.width() * 0.14
    box = rect.adjusted(margin, margin, -margin, -margin)
    path = QPainterPath()
    radius = box.height() * 0.34
    path.moveTo(box.left() + radius, box.top())
    path.lineTo(box.right() - radius, box.top())
    path.quadTo(box.right(), box.top(), box.right(), box.top() + radius)
    path.lineTo(box.right(), box.center().y())
    head = QPainterPath()
    head.moveTo(box.right(), box.center().y() + box.height() * 0.18)
    head.lineTo(box.right() - box.width() * 0.12, box.center().y())
    head.lineTo(box.right() + box.width() * 0.12, box.center().y())
    head.closeSubpath()
    paths = [path, head]
    if one:
        digit = QPainterPath()
        digit.moveTo(box.center().x() - box.width() * 0.03, box.center().y() - box.height() * 0.06)
        digit.lineTo(box.center().x() + box.width() * 0.04, box.center().y() - box.height() * 0.16)
        digit.lineTo(box.center().x() + box.width() * 0.04, box.center().y() + box.height() * 0.16)
        paths.append(digit)
    return paths


def _shuffle_paths(rect: QRectF) -> list[QPainterPath]:
    margin = rect.width() * 0.14
    box = rect.adjusted(margin, margin, -margin, -margin)
    first = QPainterPath()
    first.moveTo(box.left(), box.top() + box.height() * 0.22)
    first.lineTo(box.left() + box.width() * 0.34, box.top() + box.height() * 0.22)
    first.lineTo(box.right() - box.width() * 0.18, box.bottom() - box.height() * 0.22)
    first.lineTo(box.right(), box.bottom() - box.height() * 0.22)
    second = QPainterPath()
    second.moveTo(box.left(), box.bottom() - box.height() * 0.22)
    second.lineTo(box.left() + box.width() * 0.34, box.bottom() - box.height() * 0.22)
    second.lineTo(box.right() - box.width() * 0.18, box.top() + box.height() * 0.22)
    second.lineTo(box.right(), box.top() + box.height() * 0.22)
    arrow_top = QPainterPath()
    arrow_top.moveTo(box.right() - box.width() * 0.12, box.top() + box.height() * 0.10)
    arrow_top.lineTo(box.right(), box.top() + box.height() * 0.22)
    arrow_top.lineTo(box.right() - box.width() * 0.12, box.top() + box.height() * 0.34)
    arrow_bottom = QPainterPath()
    arrow_bottom.moveTo(box.right() - box.width() * 0.12, box.bottom() - box.height() * 0.34)
    arrow_bottom.lineTo(box.right(), box.bottom() - box.height() * 0.22)
    arrow_bottom.lineTo(box.right() - box.width() * 0.12, box.bottom() - box.height() * 0.10)
    return [first, second, arrow_top, arrow_bottom]


def _folder_path(rect: QRectF) -> QPainterPath:
    box = rect.adjusted(rect.width() * 0.14, rect.height() * 0.22, -rect.width() * 0.14, -rect.height() * 0.20)
    path = QPainterPath()
    tab_height = box.height() * 0.26
    path.moveTo(box.left(), box.top() + tab_height)
    path.lineTo(box.left() + box.width() * 0.36, box.top() + tab_height)
    path.lineTo(box.left() + box.width() * 0.44, box.top())
    path.lineTo(box.left() + box.width() * 0.72, box.top())
    path.lineTo(box.left() + box.width() * 0.72, box.top() + tab_height)
    path.lineTo(box.right(), box.top() + tab_height)
    path.lineTo(box.right(), box.bottom())
    path.lineTo(box.left(), box.bottom())
    path.closeSubpath()
    return path


def _search_path(rect: QRectF) -> list[QPainterPath]:
    radius = rect.width() * 0.26
    center = QPointF(rect.center().x() - rect.width() * 0.06, rect.center().y() - rect.height() * 0.06)
    circle = QPainterPath()
    circle.addEllipse(center, radius, radius)
    handle = QPainterPath()
    start = QPointF(center.x() + radius * 0.72, center.y() + radius * 0.72)
    handle.moveTo(start)
    handle.lineTo(start.x() + rect.width() * 0.18, start.y() + rect.height() * 0.18)
    return [circle, handle]


def _settings_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.16, rect.height() * 0.16, -rect.width() * 0.16, -rect.height() * 0.16)
    outer = QPainterPath()
    outer.addEllipse(box)
    inner = QPainterPath()
    inner.addEllipse(box.center(), box.width() * 0.16, box.height() * 0.16)
    return [outer, inner]


def _queue_path(rect: QRectF) -> list[QPainterPath]:
    paths = []
    box = rect.adjusted(rect.width() * 0.18, rect.height() * 0.22, -rect.width() * 0.18, -rect.height() * 0.22)
    for index in range(3):
        line = QPainterPath()
        y = box.top() + index * box.height() / 2.4
        line.moveTo(box.left(), y)
        line.lineTo(box.right(), y)
        paths.append(line)
    return paths


def _library_path(rect: QRectF) -> list[QPainterPath]:
    paths = []
    box = rect.adjusted(rect.width() * 0.18, rect.height() * 0.18, -rect.width() * 0.18, -rect.height() * 0.18)
    widths = (0.30, 0.55, 0.78)
    for index, ratio in enumerate(widths):
        bar = QPainterPath()
        height = box.height()
        bar.addRoundedRect(QRectF(box.left() + box.width() * (0.05 + index * 0.32), box.bottom() - height * ratio, box.width() * 0.16, height * ratio), 2.0, 2.0)
        paths.append(bar)
    return paths


def _chip_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.20, rect.height() * 0.24, -rect.width() * 0.20, -rect.height() * 0.24)
    body = QPainterPath()
    body.addRoundedRect(box, 2.4, 2.4)
    paths = [body]
    for offset in (0.25, 0.5, 0.75):
        pin = QPainterPath()
        pin.moveTo(box.left() + box.width() * offset, box.top())
        pin.lineTo(box.left() + box.width() * offset, box.top() - rect.height() * 0.10)
        paths.append(pin)
        pin2 = QPainterPath()
        pin2.moveTo(box.left() + box.width() * offset, box.bottom())
        pin2.lineTo(box.left() + box.width() * offset, box.bottom() + rect.height() * 0.10)
        paths.append(pin2)
    return paths


def _refresh_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.20, rect.height() * 0.20, -rect.width() * 0.20, -rect.height() * 0.20)
    arc = QPainterPath()
    arc.arcMoveTo(box, 40)
    arc.arcTo(box, 40, 290)
    head = QPainterPath()
    head.moveTo(box.right() - box.width() * 0.02, box.top() + box.height() * 0.05)
    head.lineTo(box.right() - box.width() * 0.26, box.top() + box.height() * 0.12)
    head.lineTo(box.right() + box.width() * 0.02, box.top() + box.height() * 0.32)
    head.closeSubpath()
    return [arc, head]


def _close_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.30, rect.height() * 0.30, -rect.width() * 0.30, -rect.height() * 0.30)
    first = QPainterPath()
    first.moveTo(box.topLeft())
    first.lineTo(box.bottomRight())
    second = QPainterPath()
    second.moveTo(box.topRight())
    second.lineTo(box.bottomLeft())
    return [first, second]


def _check_path(rect: QRectF) -> QPainterPath:
    box = rect.adjusted(rect.width() * 0.26, rect.height() * 0.28, -rect.width() * 0.26, -rect.height() * 0.28)
    path = QPainterPath()
    path.moveTo(box.left(), box.center().y())
    path.lineTo(box.left() + box.width() * 0.36, box.bottom())
    path.lineTo(box.right(), box.top())
    return path


def _power_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.22, rect.height() * 0.22, -rect.width() * 0.22, -rect.height() * 0.22)
    arc = QPainterPath()
    arc.arcMoveTo(box, 110)
    arc.arcTo(box, 110, 320)
    stem = QPainterPath()
    stem.moveTo(box.center().x(), box.top() - rect.height() * 0.04)
    stem.lineTo(box.center().x(), box.center().y())
    return [arc, stem]


def _stop_path(rect: QRectF) -> QPainterPath:
    box = rect.adjusted(rect.width() * 0.30, rect.height() * 0.30, -rect.width() * 0.30, -rect.height() * 0.30)
    path = QPainterPath()
    path.addRoundedRect(box, 2.0, 2.0)
    return path


def _info_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.24, rect.height() * 0.24, -rect.width() * 0.24, -rect.height() * 0.24)
    circle = QPainterPath()
    circle.addEllipse(box)
    stem = QPainterPath()
    stem.moveTo(box.center().x(), box.center().y() - box.height() * 0.02)
    stem.lineTo(box.center().x(), box.center().y() + box.height() * 0.24)
    dot = QPainterPath()
    dot.addEllipse(QPointF(box.center().x(), box.center().y() - box.height() * 0.22), 1.2, 1.2)
    return [circle, stem, dot]


def _eject_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.22, rect.height() * 0.22, -rect.width() * 0.22, -rect.height() * 0.22)
    triangle = QPainterPath()
    triangle.moveTo(box.center().x(), box.top())
    triangle.lineTo(box.right(), box.center().y())
    triangle.lineTo(box.left(), box.center().y())
    triangle.closeSubpath()
    base = QPainterPath()
    base.addRoundedRect(QRectF(box.left(), box.bottom() - box.height() * 0.12, box.width(), box.height() * 0.12), 1.2, 1.2)
    return [triangle, base]


def _volume_off_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.20, rect.height() * 0.28, -rect.width() * 0.20, -rect.height() * 0.28)
    speaker = QPainterPath()
    speaker.moveTo(box.left(), box.top() + box.height() * 0.30)
    speaker.lineTo(box.left() + box.width() * 0.22, box.top() + box.height() * 0.30)
    speaker.lineTo(box.left() + box.width() * 0.50, box.top())
    speaker.lineTo(box.left() + box.width() * 0.50, box.bottom())
    speaker.lineTo(box.left() + box.width() * 0.22, box.top() + box.height() * 0.70)
    speaker.lineTo(box.left(), box.top() + box.height() * 0.70)
    speaker.closeSubpath()
    cross_a = QPainterPath()
    cross_a.moveTo(box.left() + box.width() * 0.60, box.top() + box.height() * 0.32)
    cross_a.lineTo(box.right(), box.bottom() - box.height() * 0.32)
    cross_b = QPainterPath()
    cross_b.moveTo(box.right(), box.top() + box.height() * 0.32)
    cross_b.lineTo(box.left() + box.width() * 0.60, box.bottom() - box.height() * 0.32)
    return [speaker, cross_a, cross_b]


def _open_path(rect: QRectF) -> list[QPainterPath]:
    return [_folder_path(rect)]


def _save_path(rect: QRectF) -> list[QPainterPath]:
    box = rect.adjusted(rect.width() * 0.22, rect.height() * 0.20, -rect.width() * 0.22, -rect.height() * 0.20)
    body = QPainterPath()
    body.addRoundedRect(box, 2.0, 2.0)
    slot = QPainterPath()
    slot.addRect(QRectF(box.left() + box.width() * 0.28, box.top(), box.width() * 0.44, box.height() * 0.32))
    label = QPainterPath()
    label.addRect(QRectF(box.left() + box.width() * 0.18, box.bottom() - box.height() * 0.34, box.width() * 0.64, box.height() * 0.22))
    return [body, slot, label]


# --------------------------------------------------------------------- caching
# An icon path depends only on the (stable) icon box, so rebuilding the geometry
# on every paintEvent would allocate inside the hot path of every hover
# animation.  The generators are wrapped once at import time and reused.
_PATH_CACHE_LIMIT = 256


def _cached_paths(generator):
    cache: dict = {}

    def wrapper(rect: QRectF, *extra, **options):
        key = (
            round(rect.x(), 2),
            round(rect.y(), 2),
            round(rect.width(), 2),
            round(rect.height(), 2),
            extra,
            tuple(sorted(options.items())),
        )
        value = cache.get(key)
        if value is None:
            if len(cache) >= _PATH_CACHE_LIMIT:
                cache.clear()
            value = generator(rect, *extra, **options)
            cache[key] = value
        return value

    wrapper.__name__ = getattr(generator, "__name__", "cached_paths")
    wrapper.__doc__ = generator.__doc__
    return wrapper


for _generator_name in (
    "_play_path",
    "_pause_paths",
    "_skip_path",
    "_repeat_paths",
    "_shuffle_paths",
    "_folder_path",
    "_search_path",
    "_settings_path",
    "_queue_path",
    "_library_path",
    "_chip_path",
    "_refresh_path",
    "_close_path",
    "_check_path",
    "_power_path",
    "_stop_path",
    "_info_path",
    "_eject_path",
    "_volume_off_path",
    "_save_path",
):
    globals()[_generator_name] = _cached_paths(globals()[_generator_name])
del _generator_name


FILLED_ICONS = {"play", "stop", "previous", "next", "repeat", "repeat_one"}


def paint_icon(
    painter: QPainter,
    name: str,
    rect: QRectF,
    color: QColor,
    thickness: float = 1.7,
    opacity: float = 1.0,
) -> None:
    """Paint ``name`` inside ``rect`` using the current painter transform."""

    if rect.width() <= 0 or rect.height() <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen_color = QColor(color)
    pen_color.setAlphaF(max(0.0, min(1.0, opacity)) * color.alphaF())
    pen = QPen(pen_color)
    pen.setWidthF(thickness)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    fill = name in FILLED_ICONS
    if fill:
        painter.setBrush(pen_color)
        painter.setPen(Qt.PenStyle.NoPen)

    if name == "play":
        painter.drawPath(_play_path(rect))
    elif name == "pause":
        for path in _pause_paths(rect):
            painter.setBrush(pen_color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)
    elif name == "stop":
        painter.setBrush(pen_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(_stop_path(rect))
    elif name in ("previous", "next"):
        for index, path in enumerate(_skip_path(rect, forward=name == "next")):
            painter.setBrush(pen_color if index == 0 else pen_color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)
    elif name in ("repeat", "repeat_one"):
        for index, path in enumerate(_repeat_paths(rect, one=name == "repeat_one")):
            painter.setBrush(Qt.BrushStyle.NoBrush if index == 0 else pen_color)
            painter.setPen(pen if index == 0 else Qt.PenStyle.NoPen)
            painter.drawPath(path)
    elif name == "shuffle":
        for index, path in enumerate(_shuffle_paths(rect)):
            painter.setBrush(Qt.BrushStyle.NoBrush if index < 2 else pen_color)
            painter.setPen(pen if index < 2 else Qt.PenStyle.NoPen)
            painter.drawPath(path)
    elif name in ("folder", "open"):
        painter.drawPath(_folder_path(rect))
    elif name == "search":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in _search_path(rect):
            painter.drawPath(path)
    elif name == "settings":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in _settings_path(rect):
            painter.drawPath(path)
    elif name == "queue":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in _queue_path(rect):
            painter.drawPath(path)
    elif name == "library":
        painter.setBrush(pen_color)
        painter.setPen(Qt.PenStyle.NoPen)
        for path in _library_path(rect):
            painter.drawPath(path)
    elif name == "chip":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in _chip_path(rect):
            painter.drawPath(path)
    elif name == "refresh":
        for index, path in enumerate(_refresh_path(rect)):
            painter.setBrush(Qt.BrushStyle.NoBrush if index == 0 else pen_color)
            painter.setPen(pen if index == 0 else Qt.PenStyle.NoPen)
            painter.drawPath(path)
    elif name == "close":
        for path in _close_path(rect):
            painter.drawPath(path)
    elif name == "check":
        painter.drawPath(_check_path(rect))
    elif name == "power":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in _power_path(rect):
            painter.drawPath(path)
    elif name == "info":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in _info_path(rect):
            painter.drawPath(path)
    elif name == "eject":
        for index, path in enumerate(_eject_path(rect)):
            painter.setBrush(Qt.BrushStyle.NoBrush if index == 0 else pen_color)
            painter.setPen(pen if index == 0 else Qt.PenStyle.NoPen)
            painter.drawPath(path)
    elif name == "volume_off":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in _volume_off_path(rect):
            painter.drawPath(path)
    elif name == "save":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in _save_path(rect):
            painter.drawPath(path)
    else:
        painter.drawEllipse(rect.adjusted(3, 3, -3, -3))
    painter.restore()


def logo_path(rect: QRectF) -> list[QPainterPath]:
    """The True HQ Audio mark: a diamond with an embedded waveform."""

    paths: list[QPainterPath] = []
    diamond = QPainterPath()
    diamond.moveTo(rect.center().x(), rect.top())
    diamond.lineTo(rect.right(), rect.center().y())
    diamond.lineTo(rect.center().x(), rect.bottom())
    diamond.lineTo(rect.left(), rect.center().y())
    diamond.closeSubpath()
    paths.append(diamond)
    wave = QPainterPath()
    left = rect.left() + rect.width() * 0.24
    width = rect.width() * 0.52
    middle = rect.center().y()
    wave.moveTo(left, middle)
    wave.lineTo(left + width * 0.16, middle - rect.height() * 0.18)
    wave.lineTo(left + width * 0.34, middle + rect.height() * 0.18)
    wave.lineTo(left + width * 0.52, middle - rect.height() * 0.10)
    wave.lineTo(left + width * 0.70, middle + rect.height() * 0.10)
    wave.lineTo(left + width, middle)
    paths.append(wave)
    return paths
