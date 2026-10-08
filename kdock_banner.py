#!/usr/bin/python3
import base64
import gzip
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QImageReader, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QGraphicsBlurEffect, QGraphicsPixmapItem,
    QGraphicsScene, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSlider, QSpinBox, QVBoxLayout,
    QWidget,
)

APP_NAME = "Dock Banner"
APP_ID = "kdock-banner"
APP_VERSION = "1.0.0"
THEME_PREFIX = "DockBanner"
USER_THEMES = Path.home() / ".local/share/plasma/desktoptheme"
SYSTEM_THEMES = Path("/usr/share/plasma/desktoptheme")
CONFIG = Path.home() / ".config/kdockbanner.json"
DROP_DIR = Path.home() / ".local/share/kdock-banner"
RENDER_SCALE = 2
VARIANTS = ["widgets", "solid/widgets", "translucent/widgets"]
FRAME_ID = re.compile(
    r"^((north|south|east|west)-)?(mask-)?((north|south|east|west)-)?"
    r"(top|bottom|left|right|center|topleft|topright|bottomleft|bottomright|"
    r"hint-tile-center|hint-stretch-borders|hint-compose-over-border)$"
)
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
for _p, _u in {"": SVG_NS, "xlink": XLINK_NS,
               "inkscape": "http://www.inkscape.org/namespaces/inkscape",
               "sodipodi": "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"}.items():
    ET.register_namespace(_p, _u)


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def current_theme():
    out = run("kreadconfig6", "--file", "plasmarc", "--group", "Theme", "--key", "name").stdout.strip()
    return out or "default"


def theme_dir(name):
    for root in (USER_THEMES, SYSTEM_THEMES):
        if (root / name).is_dir():
            return root / name
    return SYSTEM_THEMES / "default"


def qdbus():
    for name in ("qdbus6", "qdbus-qt6", "/usr/lib/qt6/bin/qdbus", "/usr/lib64/qt6/bin/qdbus", "qdbus"):
        if shutil.which(name):
            return name
    return "qdbus6"


def list_panels():
    script = ('panels().forEach(p => print(JSON.stringify({id: p.id, thickness: p.height, length: p.length, '
              'min: p.minimumLength, max: p.maximumLength, location: p.location, floating: p.floating}) + "\\n"))')
    res = run(qdbus(), "org.kde.plasmashell", "/PlasmaShell", "org.kde.PlasmaShell.evaluateScript", script)
    panels = []
    for line in res.stdout.splitlines():
        try:
            p = json.loads(line)
        except ValueError:
            continue
        length = min(max(p["length"], p.get("min") or 0), p.get("max") or p["length"])
        p["length"] = length - (16 if p.get("floating") else 0)
        panels.append(p)
    return panels


def blur_image(img, radius):
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(img))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    out = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    p = QPainter(out)
    scene.render(p, QRectF(out.rect()), QRectF(img.rect()))
    p.end()
    return out


def render_banner(src, crop, width, height, scale, blur, dim, opacity, radius):
    pw, ph = round(width * scale), round(height * scale)
    margin = int(blur * scale * 3)
    base = QImage(pw + 2 * margin, ph + 2 * margin, QImage.Format_ARGB32_Premultiplied)
    base.fill(Qt.black)
    p = QPainter(base)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.drawImage(QRectF(base.rect()), src, crop)
    p.end()
    if blur:
        base = blur_image(base, blur * scale)

    out = QImage(pw, ph, QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.black)
    p = QPainter(out)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    p.drawImage(0, 0, base, margin, margin, pw, ph)
    if dim:
        p.fillRect(out.rect(), QColor(0, 0, 0, round(255 * dim)))
    mask = QImage(pw, ph, QImage.Format_ARGB32_Premultiplied)
    mask.fill(Qt.transparent)
    mp = QPainter(mask)
    mp.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(mask.rect()), radius * scale, radius * scale)
    mp.fillPath(path, QColor(0, 0, 0, round(255 * opacity)))
    mp.end()
    p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    p.drawImage(0, 0, mask)
    p.end()
    return out


def png_data_uri(img):
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(bytes(buf.data())).decode()


def read_svg(path):
    data = path.read_bytes()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def find_svg(directory, name):
    for ext in (".svgz", ".svg"):
        if (directory / (name + ext)).exists():
            return directory / (name + ext)
    return None


def build_panel_svg(original, banner, scale, radius):
    root = ET.fromstring(original)
    parents = {c: p for p in root.iter() for c in p}
    for el in list(root.iter()):
        if FRAME_ID.match(el.get("id", "")) and el in parents:
            parents[el].remove(el)

    vb = [float(v) for v in (root.get("viewBox") or "").replace(",", " ").split()] or None
    w0 = float(re.sub(r"[^\d.]", "", root.get("width", "")) or (vb[2] if vb else 100))
    h0 = float(re.sub(r"[^\d.]", "", root.get("height", "")) or (vb[3] if vb else 100))
    vx, vy, vw, vh = vb if vb else (0, 0, w0, h0)
    sx, sy = w0 / vw, h0 / vh

    pw, ph = banner.width(), banner.height()
    r = max(1, round(radius * scale))
    xs, ys = [0, r, pw - r, pw], [0, r, ph - r, ph]
    names = [["topleft", "top", "topright"], ["left", "center", "right"], ["bottomleft", "bottom", "bottomright"]]
    ox, oy = vx, vy + vh + 10
    group = ET.SubElement(root, f"{{{SVG_NS}}}g", {"id": "dockbanner"})
    for row in range(3):
        for col in range(3):
            x, y = xs[col], ys[row]
            w, h = xs[col + 1] - x, ys[row + 1] - y
            attrs = {
                "x": f"{ox + x / scale / sx:.4f}", "y": f"{oy + y / scale / sy:.4f}",
                "width": f"{w / scale / sx:.4f}", "height": f"{h / scale / sy:.4f}",
            }
            ET.SubElement(group, f"{{{SVG_NS}}}image", {
                "id": names[row][col], "preserveAspectRatio": "none",
                f"{{{XLINK_NS}}}href": png_data_uri(banner.copy(x, y, w, h)), **attrs,
            })
            mask = {"id": "mask-" + names[row][col], "style": "fill:#000000", **attrs}
            mx, my = float(attrs["x"]), float(attrs["y"])
            mw, mh = float(attrs["width"]), float(attrs["height"])
            if row != 1 and col != 1:
                cx = mx + (mw if col == 0 else 0)
                cy = my + (mh if row == 0 else 0)
                ex = mx if col == 0 else mx + mw
                ey = my if row == 0 else my + mh
                d = f"M{cx},{cy} L{ex},{cy} A{mw},{mh} 0 0 {1 if (row == 0) == (col == 0) else 0} {cx},{ey} Z"
                ET.SubElement(group, f"{{{SVG_NS}}}path", {"id": mask["id"], "style": mask["style"], "d": d})
            else:
                ET.SubElement(group, f"{{{SVG_NS}}}rect", mask)
    ET.SubElement(group, f"{{{SVG_NS}}}rect", {
        "id": "hint-stretch-borders", "x": f"{ox}", "y": f"{oy + ph / scale / sy + 5}",
        "width": "1", "height": "1", "style": "fill:#000000;opacity:0",
    })

    total_h = oy + ph / scale / sy + 10 - vy
    total_w = max(vw, pw / scale / sx + 10)
    root.set("viewBox", f"{vx} {vy} {total_w} {total_h}")
    root.set("width", f"{total_w * sx}")
    root.set("height", f"{total_h * sy}")
    return ET.tostring(root, encoding="unicode")


def install_theme(banner, scale, radius):
    cfg = load_config()
    cur = current_theme()
    if cur.startswith(THEME_PREFIX):
        base = cfg.get("base_theme", "default")
    else:
        base = cur
        cfg["base_theme"] = base
        save_config(cfg)

    src_dir = theme_dir(base)
    original_path = find_svg(src_dir / "widgets", "panel-background") or \
        find_svg(SYSTEM_THEMES / "default/widgets", "panel-background")
    svg = build_panel_svg(read_svg(original_path), banner, scale, radius)

    name = f"{THEME_PREFIX}-A" if cur != f"{THEME_PREFIX}-A" else f"{THEME_PREFIX}-B"
    dst = USER_THEMES / name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src_dir, dst, symlinks=False)
    (dst / "metadata.desktop").unlink(missing_ok=True)
    (dst / "metadata.json").write_text(json.dumps({
        "KPlugin": {"Id": name, "Name": f"{APP_NAME} ({base})",
                    "Description": f"{base} with an image dock", "License": "LGPL", "Version": "1.0"},
        "X-Plasma-API": "5.0",
    }, indent=4))
    for variant in VARIANTS:
        d = dst / variant
        d.mkdir(parents=True, exist_ok=True)
        for ext in (".svgz", ".svg"):
            (d / ("panel-background" + ext)).unlink(missing_ok=True)
        (d / "panel-background.svg").write_text(svg)

    (Path.home() / f".cache/plasma_theme_{name}.kcache").unlink(missing_ok=True)
    res = run("plasma-apply-desktoptheme", name)
    for old in USER_THEMES.glob(f"{THEME_PREFIX}-*"):
        if old.name != name:
            shutil.rmtree(old, ignore_errors=True)
    return res


def restore_theme():
    base = load_config().get("base_theme")
    res = None
    if current_theme().startswith(THEME_PREFIX):
        if not base:
            return None
        res = run("plasma-apply-desktoptheme", base)
    for old in USER_THEMES.glob(f"{THEME_PREFIX}-*"):
        shutil.rmtree(old, ignore_errors=True)
    return res


class CropView(QWidget):
    moved = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.image = None
        self.crop = QRectF()
        self.drag = None
        self.dropping = False
        self.cache = (None, None)
        self.setMinimumHeight(260)
        self.setCursor(Qt.OpenHandCursor)

    def set_state(self, image, crop):
        self.image, self.crop = image, crop
        self.update()

    def set_dropping(self, on):
        self.dropping = on
        self.update()

    def _geometry(self):
        iw, ih = self.image.width(), self.image.height()
        s = min(self.width() / iw, self.height() / ih)
        return s, (self.width() - iw * s) / 2, (self.height() - ih * s) / 2

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#000000"))
        if not self.image:
            p.setPen(QColor("#888888"))
            p.drawText(self.rect(), Qt.AlignCenter, "Drop an image here or click Choose Image…")
        else:
            s, ox, oy = self._geometry()
            target = QRectF(ox, oy, self.image.width() * s, self.image.height() * s)
            key = (self.image.cacheKey(), target.size().toSize())
            if self.cache[0] != key:
                self.cache = (key, QPixmap.fromImage(self.image.scaled(
                    key[1], Qt.IgnoreAspectRatio, Qt.SmoothTransformation)))
            p.drawPixmap(target.topLeft(), self.cache[1])
            box = QRectF(ox + self.crop.x() * s, oy + self.crop.y() * s,
                         self.crop.width() * s, self.crop.height() * s)
            shade = QPainterPath()
            shade.addRect(target)
            inner = QPainterPath()
            inner.addRect(box)
            p.fillPath(shade.subtracted(inner), QColor(0, 0, 0, 140))
            p.setPen(QPen(QColor("#1ec8e6"), 2))
            p.drawRect(box)
        if self.dropping:
            p.fillRect(self.rect(), QColor(30, 200, 230, 60))
            p.setPen(QPen(QColor("#1ec8e6"), 3, Qt.DashLine))
            p.drawRect(QRectF(self.rect()).adjusted(2, 2, -2, -2))
            p.setPen(QColor("#ffffff"))
            p.drawText(self.rect(), Qt.AlignCenter, "Drop to use this image")

    def mousePressEvent(self, e):
        if self.image:
            self.drag = e.position()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self.drag is None:
            return
        s, _, _ = self._geometry()
        d = (e.position() - self.drag) / s
        self.drag = e.position()
        free_x = self.image.width() - self.crop.width()
        free_y = self.image.height() - self.crop.height()
        nx = (self.crop.x() + d.x()) / free_x if free_x > 0.5 else 0.5
        ny = (self.crop.y() + d.y()) / free_y if free_y > 0.5 else 0.5
        self.moved.emit(min(max(nx, 0), 1), min(max(ny, 0), 1))

    def mouseReleaseEvent(self, _):
        self.drag = None
        self.setCursor(Qt.OpenHandCursor)


def slider(lo, hi, value):
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    return s


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(760, 720)
        self.cfg = load_config()
        self.image = None
        self.panels = []

        title = QLabel("Adjust Dock Banner Area")
        title.setStyleSheet("font-size: 16pt; font-weight: bold;")
        title.setAlignment(Qt.AlignCenter)
        hint = QLabel("Drag the box or use the sliders to choose what shows on the dock")
        hint.setAlignment(Qt.AlignCenter)
        pal = hint.palette()
        pal.setColor(QPalette.WindowText, pal.color(QPalette.PlaceholderText))
        hint.setPalette(pal)

        self.choose = QPushButton("Choose Image…")
        self.file_label = QLabel("No image selected")
        top = QHBoxLayout()
        top.addWidget(self.choose)
        top.addWidget(self.file_label, 1)

        self.view = CropView()
        self.pos_y = slider(0, 1000, 500)
        self.pos_x = slider(0, 1000, 500)
        self.zoom = slider(100, 400, 100)
        self.blur = slider(0, 60, 0)
        self.dim = slider(0, 80, 0)
        self.opacity = slider(20, 100, 100)
        self.radius = slider(0, 40, 14)

        self.panel_box = QComboBox()
        self.refresh = QPushButton("Refresh")
        self.width_spin = QSpinBox()
        self.width_spin.setRange(16, 10000)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(16, 1000)
        panel_row = QHBoxLayout()
        panel_row.addWidget(self.panel_box, 1)
        panel_row.addWidget(self.refresh)
        size_row = QHBoxLayout()
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height_spin)
        size_row.addStretch()

        form = QFormLayout()
        form.addRow("Vertical position:", self.pos_y)
        form.addRow("Horizontal position:", self.pos_x)
        form.addRow("Zoom:", self.zoom)
        form.addRow("Blur:", self.blur)
        form.addRow("Darken:", self.dim)
        form.addRow("Opacity:", self.opacity)
        form.addRow("Corner radius:", self.radius)
        form.addRow("Panel:", panel_row)
        form.addRow("Banner size (px):", size_row)

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(70)
        self.preview.setStyleSheet("background: #2a2a2e; border-radius: 8px;")

        self.restore = QPushButton("Restore Original")
        self.apply = QPushButton("Apply")
        self.apply.setDefault(True)
        for b in (self.apply, self.restore):
            b.setMinimumHeight(36)
        buttons = QHBoxLayout()
        buttons.addWidget(self.restore, 1)
        buttons.addWidget(self.apply, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        layout.addLayout(form)
        layout.addWidget(QLabel("Preview:"))
        layout.addWidget(self.preview)
        layout.addLayout(buttons)

        self.setAcceptDrops(True)
        for s in (self.pos_y, self.pos_x, self.zoom, self.blur, self.dim, self.opacity, self.radius):
            s.valueChanged.connect(self.update_preview)
        self.width_spin.valueChanged.connect(self.update_preview)
        self.height_spin.valueChanged.connect(self.update_preview)
        self.view.moved.connect(self.on_drag)
        self.choose.clicked.connect(self.pick_image)
        self.refresh.clicked.connect(self.load_panels)
        self.panel_box.currentIndexChanged.connect(self.on_panel)
        self.apply.clicked.connect(self.on_apply)
        self.restore.clicked.connect(self.on_restore)

        self.load_panels()
        self.load_settings()

    def load_settings(self):
        s = self.cfg.get("settings", {})
        for key, widget in self.sliders().items():
            if key in s:
                widget.setValue(s[key])
        if s.get("image") and Path(s["image"]).exists():
            self.set_image(s["image"])

    def sliders(self):
        return {"pos_y": self.pos_y, "pos_x": self.pos_x, "zoom": self.zoom, "blur": self.blur,
                "dim": self.dim, "opacity": self.opacity, "radius": self.radius}

    def load_panels(self):
        self.panels = list_panels()
        self.panel_box.blockSignals(True)
        self.panel_box.clear()
        for p in self.panels:
            kind = "floating " if p.get("floating") else ""
            self.panel_box.addItem(f"Panel {p['id']} — {kind}{p['location']} ({p['length']}×{p['thickness']})")
        self.panel_box.addItem("Custom size")
        self.panel_box.blockSignals(False)
        self.on_panel(0)

    def on_panel(self, index):
        if index < len(self.panels):
            p = self.panels[index]
            vertical = p["location"] in ("left", "right")
            w, h = (p["thickness"], p["length"]) if vertical else (p["length"], p["thickness"])
            self.width_spin.setValue(w)
            self.height_spin.setValue(h)
            self.radius.setMaximum(max(1, min(w, h) // 2))
        self.update_preview()

    def pick_image(self):
        start = self.cfg.get("settings", {}).get("image", str(Path.home() / "Pictures"))
        path, _ = QFileDialog.getOpenFileName(self, "Choose Image", start,
                                              "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.avif *.jxl)")
        if path:
            self.set_image(path)

    def set_image(self, path):
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        img = reader.read()
        if img.isNull():
            QMessageBox.warning(self, APP_NAME, f"Could not open image:\n{reader.errorString()}")
            return
        self.image = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.image_path = path
        self.file_label.setText(Path(path).name)
        self.update_preview()

    def on_drag(self, x, y):
        for s, v in ((self.pos_x, x), (self.pos_y, y)):
            s.blockSignals(True)
            s.setValue(round(v * 1000))
            s.blockSignals(False)
        self.update_preview()

    def dropped_path(self, mime):
        for url in mime.urls():
            if url.isLocalFile() and not QImageReader.imageFormat(url.toLocalFile()).isEmpty():
                return url.toLocalFile()
        if mime.hasImage():
            img = QImage(mime.imageData())
            if not img.isNull():
                DROP_DIR.mkdir(parents=True, exist_ok=True)
                path = DROP_DIR / "dropped.png"
                img.save(str(path))
                return str(path)
        return None

    def dragEnterEvent(self, e):
        mime = e.mimeData()
        if any(u.isLocalFile() for u in mime.urls()) or mime.hasImage():
            e.acceptProposedAction()
            self.view.set_dropping(True)

    def dragLeaveEvent(self, e):
        self.view.set_dropping(False)

    def dropEvent(self, e):
        self.view.set_dropping(False)
        path = self.dropped_path(e.mimeData())
        if path:
            e.acceptProposedAction()
            self.set_image(path)
        else:
            QMessageBox.warning(self, APP_NAME, "That doesn't look like an image file.")

    def crop_rect(self):
        iw, ih = self.image.width(), self.image.height()
        aspect = self.width_spin.value() / self.height_spin.value()
        cw, ch = (ih * aspect, ih) if iw / ih > aspect else (iw, iw / aspect)
        z = self.zoom.value() / 100
        cw, ch = cw / z, ch / z
        x = (iw - cw) * self.pos_x.value() / 1000
        y = (ih - ch) * self.pos_y.value() / 1000
        return QRectF(x, y, cw, ch)

    def make_banner(self, scale):
        return render_banner(self.image, self.crop_rect(), self.width_spin.value(), self.height_spin.value(),
                             scale, self.blur.value(), self.dim.value() / 100, self.opacity.value() / 100,
                             self.radius.value())

    def update_preview(self):
        if not self.image:
            self.view.set_state(None, QRectF())
            return
        self.view.set_state(self.image, self.crop_rect())
        avail = self.preview.width() - 20
        scale = min(1.0, avail / self.width_spin.value()) if avail > 0 else 1.0
        self.preview.setPixmap(QPixmap.fromImage(self.make_banner(scale)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update_preview()

    def on_apply(self):
        if not self.image:
            QMessageBox.information(self, APP_NAME, "Choose an image first.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = install_theme(self.make_banner(RENDER_SCALE), RENDER_SCALE, self.radius.value())
        finally:
            QApplication.restoreOverrideCursor()
        self.cfg = load_config()
        self.cfg["settings"] = {k: w.value() for k, w in self.sliders().items()} | {"image": self.image_path}
        save_config(self.cfg)
        if res.returncode != 0:
            QMessageBox.warning(self, APP_NAME, res.stderr or res.stdout)

    def on_restore(self):
        res = restore_theme()
        if res is None:
            QMessageBox.information(self, APP_NAME, "Nothing to restore yet.")
        elif res.returncode != 0:
            QMessageBox.warning(self, APP_NAME, res.stderr or res.stdout)


def app_icon():
    for path in (Path(__file__).resolve().parent / "data" / f"{APP_ID}.svg",
                 Path(__file__).resolve().parent / f"{APP_ID}.svg"):
        if path.exists():
            return QIcon.fromTheme(APP_ID, QIcon(str(path)))
    return QIcon.fromTheme(APP_ID)


def main():
    if "--version" in sys.argv:
        print(f"{APP_ID} {APP_VERSION}")
        return
    if "--restore" in sys.argv:
        res = restore_theme()
        print(res.stdout.strip() if res else "Nothing to restore.")
        return
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(app_icon())
    w = Window()
    files = [a for a in sys.argv[1:] if not a.startswith("-") and Path(a).is_file()]
    if files:
        w.set_image(files[0])
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
