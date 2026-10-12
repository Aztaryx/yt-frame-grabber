"""pygame front-end: URL + ranges in, selectable frame grid out."""
from __future__ import annotations

import atexit
import os
import platform
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from math import ceil
from pathlib import Path

import pygame

import backend
import timeparse

# ---- look -----------------------------------------------------------------
BG = (24, 26, 31)
PANEL = (33, 36, 43)
FIELD = (18, 19, 23)
BORDER = (58, 62, 72)
TEXT = (225, 228, 235)
DIM = (128, 134, 146)
ACCENT = (86, 156, 255)
ACCENT_DK = (60, 120, 210)
SEL_BG = (30, 52, 84)
BTN = (52, 56, 66)
BTN_HOVER = (68, 73, 86)
BTN_OFF = (38, 40, 47)
OK = (100, 200, 130)
ERR = (240, 100, 100)

CELL_W, CELL_H = 208, 150
THUMB_W, THUMB_H = 192, 108
GRID_TOP = 168
TOOLBAR_H = 52


def get_clipboard() -> str:
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        pass
    try:
        return pygame.scrap.get_text() or ""   # pygame-ce fallback
    except Exception:
        return ""


def fit(font: pygame.font.Font, s: str, width: int) -> str:
    if font.size(s)[0] <= width:
        return s
    while s and font.size(s + "...")[0] > width:
        s = s[:-1]
    return s + "..."


def short_err(e: Exception) -> str:
    s = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip().splitlines()
    return (s[0] if s else type(e).__name__)[:300]


def open_folder(path: Path) -> None:
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


# ---- widgets --------------------------------------------------------------
class TextField:
    def __init__(self, hint: str = "", normalize_newlines: bool = False):
        self.text = ""
        self.cur = 0
        self.focus = False
        self.hint = hint
        self.normalize_newlines = normalize_newlines
        self.rect = pygame.Rect(0, 0, 0, 0)
        self.all = False      # "select all" state
        self._off = 0
        self._font: pygame.font.Font | None = None

    def set(self, s: str) -> None:
        self.text, self.cur, self.all = s, len(s), False

    def insert(self, s: str) -> None:
        s = s.replace("\r", "")
        if self.normalize_newlines:
            s = s.replace("\n", ", ")
        else:
            s = s.replace("\n", "")
        if not s:
            return
        if self.all:
            self.text, self.cur, self.all = "", 0, False
        self.text = self.text[:self.cur] + s + self.text[self.cur:]
        self.cur += len(s)

    def click(self, x: int) -> None:
        self.all = False
        if not self._font:
            return
        inner_x = self.rect.x + 8
        best = len(self.text)
        for i in range(len(self.text) + 1):
            if inner_x + self._font.size(self.text[:i])[0] - self._off >= x:
                best = i
                break
        self.cur = best

    def key(self, e: pygame.event.Event) -> bool:
        """Handle a KEYDOWN. Returns True if Enter was pressed."""
        ctrl = e.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)
        k = e.key
        if ctrl and k == pygame.K_v:
            self.insert(get_clipboard().strip())
        elif ctrl and k == pygame.K_a:
            self.all = True
        elif k == pygame.K_BACKSPACE:
            if self.all:
                self.set("")
            elif self.cur > 0:
                self.text = self.text[:self.cur - 1] + self.text[self.cur:]
                self.cur -= 1
        elif k == pygame.K_DELETE:
            if self.all:
                self.set("")
            else:
                self.text = self.text[:self.cur] + self.text[self.cur + 1:]
        elif k == pygame.K_LEFT:
            self.cur, self.all = max(0, self.cur - 1), False
        elif k == pygame.K_RIGHT:
            self.cur, self.all = min(len(self.text), self.cur + 1), False
        elif k == pygame.K_HOME:
            self.cur, self.all = 0, False
        elif k == pygame.K_END:
            self.cur, self.all = len(self.text), False
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            return True
        return False

    def draw(self, surf: pygame.Surface, font: pygame.font.Font) -> None:
        self._font = font
        pygame.draw.rect(surf, FIELD, self.rect, border_radius=6)
        pygame.draw.rect(surf, ACCENT if self.focus else BORDER, self.rect,
                         2 if self.focus else 1, border_radius=6)
        inner = self.rect.inflate(-16, -8)
        cy = inner.centery - font.get_height() // 2

        if not self.text:
            surf.blit(font.render(self.hint, True, DIM), (inner.x, cy))
        text_w = font.size(self.text)[0]
        cx = font.size(self.text[:self.cur])[0]
        if text_w <= inner.w:
            self._off = 0
        elif cx - self._off > inner.w - 2:
            self._off = cx - inner.w + 2
        elif cx - self._off < 0:
            self._off = cx

        prev = surf.get_clip()
        surf.set_clip(inner)
        if self.all and self.text:
            pygame.draw.rect(surf, ACCENT_DK,
                             (inner.x - self._off, inner.y, text_w, inner.h))
        if self.text:
            surf.blit(font.render(self.text, True, TEXT), (inner.x - self._off, cy))
        if self.focus and (pygame.time.get_ticks() // 500) % 2 == 0:
            x = inner.x + cx - self._off
            pygame.draw.line(surf, TEXT, (x, inner.y + 2), (x, inner.bottom - 2))
        surf.set_clip(prev)


class Button:
    def __init__(self, label: str, action, primary: bool = False, toolbar: bool = False):
        self.label = label
        self.action = action
        self.primary = primary
        self.toolbar = toolbar
        self.rect = pygame.Rect(0, 0, 0, 0)
        self.enabled = True
        self.visible = True


# ---- app ------------------------------------------------------------------
class App:
    def __init__(self, url: str = "", rng: str = ""):
        pygame.init()
        pygame.key.start_text_input()
        pygame.display.set_caption("YT Frame Grabber")
        self.screen = pygame.display.set_mode((1280, 820), pygame.RESIZABLE)
        names = "segoeui,helveticaneue,helvetica,arial,dejavusans"
        self.font = pygame.font.SysFont(names, 18)
        self.small = pygame.font.SysFont(names, 14)

        self.url_field = TextField("Paste a YouTube link")
        self.range_field = TextField(
            "e.g.  87-88.5, 100-101", normalize_newlines=True
        )
        self.url_field.set(url)
        self.range_field.set(rng)
        self.url_field.focus = True

        self.btn = {
            "fetch": Button("Fetch", self.do_fetch),
            "go": Button("Extract", self.do_go, primary=True),
            "all": Button("All", self.select_all, toolbar=True),
            "none": Button("None", self.select_none, toolbar=True),
            "invert": Button("Invert", self.select_invert, toolbar=True),
            "delete": Button("Delete selected", self.delete_selected, toolbar=True),
            "keep": Button("Keep selected", self.keep_selected, toolbar=True),
            "open": Button("Open folder", self.do_open, toolbar=True),
        }
        self.L: dict[str, pygame.Rect] = {}

        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.progress: float | None = None

        self.info: backend.VideoInfo | None = None
        self.frames: list[backend.Frame] = []
        self.out_dir: Path | None = None

        self.status = "Paste a link and one or more time ranges, then hit Extract."
        self.status_kind = "info"

        self.scroll = 0
        self.geom = (0, 1)
        self.thumbs: dict[int, pygame.Surface] = {}
        self.loads_left = 0
        self.anchor: int | None = None
        self.dragging = False
        self.drag_state = True

        self.preview: int | None = None
        self._pv = None

        self.tmp = Path(tempfile.mkdtemp(prefix="ytframes_"))
        atexit.register(shutil.rmtree, self.tmp, True)

    # ---- status / background jobs ------------------------------------------
    def set_status(self, text: str, kind: str = "info") -> None:
        self.status, self.status_kind = text, kind

    def _run(self, fn) -> None:
        self.busy = True
        self.cancel.clear()
        self.progress = None

        def target():
            try:
                fn()
            except backend.Cancelled:
                self.q.put(("status", "Cancelled.", "info"))
            except Exception as e:  # noqa: BLE001
                if self.cancel.is_set():
                    self.q.put(("status", "Cancelled.", "info"))
                else:
                    self.q.put(("status", short_err(e), "error"))
            finally:
                self.q.put(("done",))

        threading.Thread(target=target, daemon=True).start()

    def _progress(self, frac, text) -> None:
        self.q.put(("progress", frac, text))

    def drain(self) -> None:
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                return
            kind = msg[0]
            if kind == "progress":
                self.progress = msg[1]
                self.set_status(msg[2], "info")
            elif kind == "info":
                self.info = msg[1]
            elif kind == "status":
                self.set_status(msg[1], msg[2])
            elif kind == "frames":
                self.load_frames(msg[1], msg[2])
            elif kind == "focus_range":
                self.url_field.focus, self.range_field.focus = False, True
            elif kind == "done":
                self.busy = False
                self.progress = None

    # ---- actions -------------------------------------------------------------
    def do_fetch(self) -> None:
        if self.busy:
            return
        url = self.url_field.text.strip()
        if not url:
            self.set_status("Paste a link first.", "error")
            return

        def job():
            self._progress(None, "Fetching video info...")
            info = backend.fetch_info(url)
            self.q.put(("info", info))
            self.q.put(("status", "Got it. Enter one or more ranges and hit Extract.", "ok"))
            self.q.put(("focus_range",))

        self._run(job)

    def do_go(self) -> None:
        if self.busy:
            self.cancel.set()
            self.set_status("Cancelling...", "info")
        else:
            self.do_extract()

    def do_extract(self) -> None:
        if self.busy:
            return
        url = self.url_field.text.strip()
        if not url:
            self.set_status("Paste a link first.", "error")
            return
        try:
            ranges = timeparse.parse_ranges(self.range_field.text)
        except timeparse.TimeParseError as e:
            self.set_status(str(e), "error")
            return
        known = self.info if (self.info and self.info.url == url) else None

        def job():
            info = known
            if info is None:
                self._progress(None, "Fetching video info...")
                info = backend.fetch_info(url)
                self.q.put(("info", info))
            bounded = []
            for start, end in ranges:
                if info.duration and start >= info.duration:
                    raise ValueError(
                        f"Start is past the end of the video "
                        f"({timeparse.fmt_dur(info.duration)})."
                    )
                bounded.append((start, min(end, info.duration))
                               if info.duration else (start, end))

            src = backend.ensure_downloaded(info, self._progress, self.cancel)
            self.q.put(("info", info))   # refresh with exact frame count

            out_dir = (backend.make_output_dir(info, *bounded[0])
                       if len(bounded) == 1
                       else backend.make_multi_output_dir(info))
            thumb_dir = self.tmp / out_dir.name
            expected = (round(info.fps * sum(end - start for start, end in bounded))
                        if info.fps else 0)
            frames = backend.extract_ranges(
                src, bounded, out_dir, thumb_dir, expected,
                self._progress, self.cancel,
            )
            self.q.put(("frames", frames, out_dir))

        self._run(job)

    def load_frames(self, frames, out_dir: Path) -> None:
        self.frames = frames
        self.out_dir = out_dir
        self.thumbs.clear()
        self.scroll = 0
        self.anchor = None
        self.preview = None
        self.set_status(
            f"{len(frames)} frames -> {out_dir.name}   |   click/drag: select, "
            "shift-click: range, right-click: preview", "ok")

    def select_all(self) -> None:
        for f in self.frames:
            f.selected = True

    def select_none(self) -> None:
        for f in self.frames:
            f.selected = False

    def select_invert(self) -> None:
        for f in self.frames:
            f.selected = not f.selected

    def _remove(self, doomed: list[backend.Frame], verb: str) -> None:
        if not doomed:
            return
        backend.delete_frames(doomed)
        gone = {id(f) for f in doomed}
        self.frames = [f for f in self.frames if id(f) not in gone]
        for f in doomed:
            self.thumbs.pop(f.index, None)
        self.select_none()
        self.anchor = None
        self.preview = None
        self.set_status(f"{verb} {len(doomed)} frames, {len(self.frames)} remain.", "ok")

    def delete_selected(self) -> None:
        self._remove([f for f in self.frames if f.selected], "Deleted")

    def keep_selected(self) -> None:
        if any(f.selected for f in self.frames):
            self._remove([f for f in self.frames if not f.selected], "Removed")

    def do_open(self) -> None:
        if self.out_dir:
            open_folder(self.out_dir)

    # ---- layout ----------------------------------------------------------------
    def layout(self) -> None:
        self.screen = pygame.display.get_surface()
        w, h = self.screen.get_size()
        m, bw = 16, 108
        bx = w - m - bw
        has = bool(self.frames)
        tb_h = TOOLBAR_H if has else 0

        self.L["url"] = pygame.Rect(76, 14, bx - 12 - 76, 34)
        self.L["range"] = pygame.Rect(76, 58, 300, 34)
        self.url_field.rect = self.L["url"]
        self.range_field.rect = self.L["range"]
        self.btn["fetch"].rect = pygame.Rect(bx, 14, bw, 34)
        self.btn["go"].rect = pygame.Rect(bx, 58, bw, 34)
        self.L["grid"] = pygame.Rect(0, GRID_TOP, w, max(0, h - GRID_TOP - tb_h))
        self.L["toolbar"] = pygame.Rect(0, h - tb_h, w, tb_h)

        y = h - tb_h + 9
        for name, x, bw_ in (("all", m, 64), ("none", m + 72, 64), ("invert", m + 144, 76)):
            self.btn[name].rect = pygame.Rect(x, y, bw_, 34)
        right = w - m
        for name, bw_ in (("open", 120), ("keep", 130), ("delete", 150)):
            self.btn[name].rect = pygame.Rect(right - bw_, y, bw_, 34)
            right -= bw_ + 8

        n_sel = sum(f.selected for f in self.frames)
        for name in ("all", "none", "invert", "delete", "keep", "open"):
            self.btn[name].visible = has
        self.btn["fetch"].enabled = not self.busy
        self.btn["go"].label = "Cancel" if self.busy else "Extract"
        self.btn["delete"].label = f"Delete selected ({n_sel})" if n_sel else "Delete selected"
        self.btn["delete"].enabled = n_sel > 0
        self.btn["keep"].enabled = n_sel > 0
        self.btn["none"].enabled = n_sel > 0

    # ---- events ----------------------------------------------------------------
    def cell_at(self, pos) -> int | None:
        g = self.L["grid"]
        if not g.collidepoint(pos) or not self.frames:
            return None
        x0, cols = self.geom
        col = (pos[0] - x0) // CELL_W
        row = (pos[1] - g.y + self.scroll) // CELL_H
        if col < 0 or col >= cols:
            return None
        i = row * cols + col
        return i if 0 <= i < len(self.frames) else None

    def handle(self, e: pygame.event.Event) -> None:
        if self.preview is not None:
            self.handle_preview(e)
            return

        if e.type == pygame.MOUSEBUTTONDOWN:
            if e.button == 1:
                for b in self.btn.values():
                    if b.visible and b.enabled and b.rect.collidepoint(e.pos):
                        b.action()
                        return
                for fld in (self.url_field, self.range_field):
                    fld.focus = fld.rect.collidepoint(e.pos)
                    if fld.focus:
                        fld.click(e.pos[0])
                i = self.cell_at(e.pos)
                if i is not None:
                    shift = pygame.key.get_mods() & pygame.KMOD_SHIFT
                    if shift and self.anchor is not None:
                        a, b = sorted((self.anchor, i))
                        for f in self.frames[a:b + 1]:
                            f.selected = True
                    else:
                        self.drag_state = not self.frames[i].selected
                        self.frames[i].selected = self.drag_state
                        self.anchor = i
                    self.dragging = True
            elif e.button == 3:
                i = self.cell_at(e.pos)
                if i is not None:
                    self.preview, self._pv = i, None

        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.dragging = False

        elif e.type == pygame.MOUSEMOTION and self.dragging:
            i = self.cell_at(e.pos)
            if i is not None:
                self.frames[i].selected = self.drag_state

        elif e.type == pygame.MOUSEWHEEL:
            self.scroll -= e.y * 70

        elif e.type == pygame.TEXTINPUT:
            for fld in (self.url_field, self.range_field):
                if fld.focus:
                    fld.insert(e.text)

        elif e.type == pygame.KEYDOWN:
            focused = self.url_field if self.url_field.focus else (
                self.range_field if self.range_field.focus else None)
            if e.key == pygame.K_ESCAPE:
                if self.busy:
                    self.cancel.set()
            elif e.key == pygame.K_TAB:
                self.url_field.focus, self.range_field.focus = (
                    self.range_field.focus, self.url_field.focus)
                if not (self.url_field.focus or self.range_field.focus):
                    self.url_field.focus = True
            elif focused is not None:
                if focused.key(e):
                    if focused is self.url_field:
                        self.do_fetch()
                    else:
                        self.do_extract()
            else:
                ctrl = e.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)
                if ctrl and e.key == pygame.K_a:
                    self.select_all()
                elif e.key in (pygame.K_DELETE, pygame.K_BACKSPACE):
                    self.delete_selected()

    def handle_preview(self, e: pygame.event.Event) -> None:
        n = len(self.frames)
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_ESCAPE:
                self.preview = None
            elif e.key == pygame.K_LEFT:
                self.preview = max(0, self.preview - 1)
            elif e.key == pygame.K_RIGHT:
                self.preview = min(n - 1, self.preview + 1)
            elif e.key == pygame.K_SPACE:
                f = self.frames[self.preview]
                f.selected = not f.selected
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            self.preview = None
        elif e.type == pygame.MOUSEWHEEL:
            self.preview = max(0, min(n - 1, self.preview - e.y))

    # ---- drawing ---------------------------------------------------------------
    def text(self, s, pos, font=None, color=TEXT) -> None:
        font = font or self.font
        self.screen.blit(font.render(s, True, color), pos)

    def draw_button(self, b: Button) -> None:
        if not b.visible:
            return
        hover = b.enabled and b.rect.collidepoint(pygame.mouse.get_pos())
        if not b.enabled:
            col = BTN_OFF
        elif b.primary:
            col = ACCENT_DK if hover else ACCENT
        else:
            col = BTN_HOVER if hover else BTN
        pygame.draw.rect(self.screen, col, b.rect, border_radius=6)
        img = self.font.render(b.label, True, TEXT if b.enabled else DIM)
        self.screen.blit(img, img.get_rect(center=b.rect.center))

    def info_line(self) -> str:
        i = self.info
        if not i:
            return ""
        fps = f"{i.fps:g} fps" if i.fps else "? fps"
        if i.exact_frames:
            frames = f"{i.exact_frames:,} frames"
        elif i.est_frames:
            frames = f"~{i.est_frames:,} frames"
        else:
            frames = "frame count unknown"
        return (f"{i.title}  |  {i.width}x{i.height} @ {fps} ({i.codec_short})"
                f"  |  {timeparse.fmt_dur(i.duration)}  |  {frames}")

    def get_thumb(self, f: backend.Frame):
        s = self.thumbs.get(f.index)
        if s is None and self.loads_left > 0:
            self.loads_left -= 1
            try:
                img = pygame.image.load(str(f.thumb)).convert()
                iw, ih = img.get_size()
                k = min(THUMB_W / iw, THUMB_H / ih)
                s = pygame.transform.smoothscale(
                    img, (max(1, int(iw * k)), max(1, int(ih * k))))
            except Exception:  # noqa: BLE001
                s = pygame.Surface((THUMB_W, THUMB_H))
                s.fill((70, 40, 40))
            self.thumbs[f.index] = s
        return s

    def draw_grid(self) -> None:
        r = self.L["grid"]
        n = len(self.frames)
        if n == 0 or r.h <= 0:
            return
        cols = max(1, r.w // CELL_W)
        x0 = r.x + (r.w - cols * CELL_W) // 2
        rows = ceil(n / cols)
        total = rows * CELL_H + 8
        self.scroll = max(0, min(self.scroll, total - r.h))
        self.geom = (x0, cols)
        self.loads_left = 40

        prev = self.screen.get_clip()
        self.screen.set_clip(r)
        r0 = int(self.scroll // CELL_H)
        r1 = min(rows - 1, int((self.scroll + r.h) // CELL_H))
        for row in range(r0, r1 + 1):
            for col in range(cols):
                i = row * cols + col
                if i >= n:
                    break
                f = self.frames[i]
                cell = pygame.Rect(x0 + col * CELL_W, r.y + row * CELL_H - self.scroll,
                                   CELL_W, CELL_H).inflate(-8, -8)
                pygame.draw.rect(self.screen, SEL_BG if f.selected else PANEL,
                                 cell, border_radius=8)
                area = pygame.Rect(cell.x + 4, cell.y + 4, THUMB_W, THUMB_H)
                s = self.get_thumb(f)
                if s:
                    self.screen.blit(s, s.get_rect(center=area.center))
                if f.selected:
                    pygame.draw.rect(self.screen, ACCENT, cell, 2, border_radius=8)
                    c = (area.right - 12, area.y + 12)
                    pygame.draw.circle(self.screen, ACCENT, c, 10)
                    pygame.draw.lines(self.screen, (255, 255, 255), False,
                                      [(c[0] - 5, c[1]), (c[0] - 1, c[1] + 4),
                                       (c[0] + 5, c[1] - 4)], 2)
                ly = cell.y + THUMB_H + 10
                self.text(f"#{f.index}", (cell.x + 8, ly), self.small, DIM)
                if f.t is not None:
                    ts = f"{f.t:.3f}s"
                    self.text(ts, (cell.right - 8 - self.small.size(ts)[0], ly),
                              self.small, TEXT)
        self.screen.set_clip(prev)

        if total > r.h:
            bar_h = max(24, r.h * r.h // total)
            bar_y = r.y + (r.h - bar_h) * self.scroll // max(1, total - r.h)
            pygame.draw.rect(self.screen, BORDER, (r.right - 7, bar_y, 4, bar_h),
                             border_radius=2)

    def draw_preview(self) -> None:
        w, h = self.screen.get_size()
        self.screen.fill((10, 11, 14))
        f = self.frames[self.preview]
        key = (f.index, w, h)
        if self._pv is None or self._pv[0] != key:
            try:
                img = pygame.image.load(str(f.path)).convert()
                iw, ih = img.get_size()
                k = min((w - 40) / iw, (h - 90) / ih)
                surf = pygame.transform.smoothscale(
                    img, (max(1, int(iw * k)), max(1, int(ih * k))))
                self._pv = (key, surf, f"{iw}x{ih}")
            except Exception as ex:  # noqa: BLE001
                self._pv = (key, None, f"Couldn't load: {short_err(ex)}")
        _, surf, meta = self._pv
        if surf:
            self.screen.blit(surf, surf.get_rect(center=(w // 2, (h - 50) // 2)))
        else:
            self.text(meta, (24, 24), self.font, ERR)

        t = f"  t = {f.t:.3f}s" if f.t is not None else ""
        cap = f"{self.preview + 1}/{len(self.frames)}   #{f.index}{t}   {meta if surf else ''}"
        self.text(cap, (20, h - 40), self.font)
        hint = "left/right: step    space: select    esc: close"
        self.text(hint, (w - 20 - self.small.size(hint)[0], h - 36), self.small, DIM)
        if f.selected:
            pygame.draw.rect(self.screen, ACCENT, (20, 16, 100, 28), border_radius=6)
            self.text("SELECTED", (30, 20), self.small)

    def draw(self) -> None:
        if self.preview is not None:
            self.draw_preview()
            return
        s = self.screen
        w, h = s.get_size()
        s.fill(BG)

        self.text("URL", (16, 21), self.font, DIM)
        self.text("Ranges", (16, 65), self.font, DIM)
        self.url_field.draw(s, self.font)
        self.range_field.draw(s, self.font)
        self.text("Separate ranges with commas, semicolons, or new lines",
                  (self.L["range"].right + 14, 66), self.small, DIM)
        for b in self.btn.values():
            self.draw_button(b)

        self.text(fit(self.font, self.info_line(), w - 32), (16, 104), self.font, DIM)
        col = {"ok": OK, "error": ERR}.get(self.status_kind, TEXT)
        self.text(fit(self.font, self.status, w - 32), (16, 130), self.font, col)

        # progress bar
        bar = pygame.Rect(0, 158, w, 3)
        if self.busy:
            pygame.draw.rect(s, PANEL, bar)
            if self.progress is not None:
                pygame.draw.rect(s, ACCENT, (0, bar.y, int(w * self.progress), bar.h))
            else:
                x = (pygame.time.get_ticks() // 4) % (w + 160) - 160
                pygame.draw.rect(s, ACCENT, (max(0, x), bar.y, 160 - max(0, -x), bar.h))

        self.draw_grid()

        if self.frames:
            tb = self.L["toolbar"]
            pygame.draw.rect(s, PANEL, tb)
            n_sel = sum(f.selected for f in self.frames)
            self.text(f"{len(self.frames)} frames, {n_sel} selected",
                      (self.btn["invert"].rect.right + 16, tb.y + 16), self.small, DIM)
            for name in ("all", "none", "invert", "delete", "keep", "open"):
                self.draw_button(self.btn[name])

    # ---- main loop -------------------------------------------------------------
    def run(self) -> None:
        clock = pygame.time.Clock()
        running = True
        while running:
            self.layout()
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    running = False
                else:
                    self.handle(e)
            self.drain()
            self.draw()
            pygame.display.flip()
            clock.tick(60)
        self.cancel.set()
        pygame.quit()
