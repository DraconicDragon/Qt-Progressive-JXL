import sys
import os
import ctypes
import ctypes.util
import threading
import time
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QVBoxLayout, QHBoxLayout, QWidget, QSlider, QLabel,
    QComboBox, QDoubleSpinBox, QPushButton
)
from PySide6.QtGui import QImage, QPixmap, QColor, QFont, QPainter
from PySide6.QtCore import Qt, QThread, Signal, QTimer

# NOTE: progressive encoded images with distance > 0.0 and distance < 0.51 have their last blocks appear red
# weird bug, might be a JXL thing, or the decode here is wrong but otherwise it seems perfectly fine?


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def list_jxl_files(directory):
    try:
        return sorted(
            f for f in os.listdir(directory)
            if f.lower().endswith(".jxl") and os.path.isfile(os.path.join(directory, f))
        )
    except OSError:
        return []


# ==========================================
# Worker Thread for Async JXL Decoding
# ==========================================
class JxlWorker(QThread):
    frame_ready = Signal(QImage)
    error_occurred = Signal(str)
    not_enough_data = Signal()

    JXL_DEC_SUCCESS = 0
    JXL_DEC_ERROR = 1
    JXL_DEC_NEED_MORE_INPUT = 2
    JXL_DEC_NEED_IMAGE_OUT_BUFFER = 5

    JXL_DEC_BASIC_INFO = 0x40
    JXL_DEC_FULL_IMAGE = 0x1000
    JXL_DEC_FRAME_PROGRESSION = 0x8000

    JXL_TYPE_UINT8 = 2
    JXL_NATIVE_ENDIAN = 0

    class JxlBasicInfo(ctypes.Structure):
        # Truncated view of libjxl's real struct — enough for xsize/ysize.
        _fields_ = [
            ("have_container", ctypes.c_int),
            ("xsize", ctypes.c_uint32),
            ("ysize", ctypes.c_uint32),
        ]

    class JxlPixelFormat(ctypes.Structure):
        _fields_ = [
            ("num_channels", ctypes.c_uint32),
            ("data_type", ctypes.c_int),
            ("endianness", ctypes.c_int),
            ("align", ctypes.c_size_t)
        ]

    def __init__(self, full_data):
        super().__init__()
        self.full_data = full_data
        self.target_bytes = len(full_data)
        # Immutable backing buffer; incremental feeds point into it
        self._c_data = (ctypes.c_uint8 * len(full_data)).from_buffer_copy(full_data)

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._running = True
        self._new_request = True

        self.lib = self._load_libjxl()
        self.threads_lib = self._load_libjxl_threads()
        self.runner = None
        self._runner_fn = None

        # Persistent decoder state (survives across _decode_to calls)
        self._dec = None
        self._pixels = None
        self._w = 0
        self._h = 0
        self._fmt = None
        self._fed_bytes = 0

    # region library loading
    def _load_libjxl(self):
        lib_path = ctypes.util.find_library("jxl")
        if not lib_path:
            return None
        lib = ctypes.CDLL(lib_path)
        lib.JxlDecoderCreate.restype = ctypes.c_void_p
        lib.JxlDecoderCreate.argtypes = [ctypes.c_void_p]
        lib.JxlDecoderDestroy.restype = None
        lib.JxlDecoderDestroy.argtypes = [ctypes.c_void_p]
        lib.JxlDecoderSubscribeEvents.restype = ctypes.c_int
        lib.JxlDecoderSubscribeEvents.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.JxlDecoderSetInput.restype = ctypes.c_int
        lib.JxlDecoderSetInput.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
        # Required before re-feeding a new buffer into an existing decoder
        lib.JxlDecoderReleaseInput.restype = None
        lib.JxlDecoderReleaseInput.argtypes = [ctypes.c_void_p]
        lib.JxlDecoderProcessInput.restype = ctypes.c_int
        lib.JxlDecoderProcessInput.argtypes = [ctypes.c_void_p]
        lib.JxlDecoderGetBasicInfo.restype = ctypes.c_int
        lib.JxlDecoderGetBasicInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.JxlDecoderImageOutBufferSize.restype = ctypes.c_int
        lib.JxlDecoderImageOutBufferSize.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.JxlPixelFormat), ctypes.POINTER(ctypes.c_size_t)]
        lib.JxlDecoderSetImageOutBuffer.restype = ctypes.c_int
        lib.JxlDecoderSetImageOutBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.JxlPixelFormat), ctypes.c_void_p, ctypes.c_size_t]
        lib.JxlDecoderSetProgressiveDetail.restype = ctypes.c_int
        lib.JxlDecoderSetProgressiveDetail.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.JxlDecoderFlushImage.restype = ctypes.c_int
        lib.JxlDecoderFlushImage.argtypes = [ctypes.c_void_p]
        lib.JxlDecoderSetParallelRunner.restype = ctypes.c_int
        lib.JxlDecoderSetParallelRunner.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        return lib

    def _load_libjxl_threads(self):
        lib_path = ctypes.util.find_library("jxl_threads")
        if not lib_path:
            return None
        lib = ctypes.CDLL(lib_path)
        lib.JxlThreadParallelRunnerCreate.restype = ctypes.c_void_p
        lib.JxlThreadParallelRunnerCreate.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.JxlThreadParallelRunnerDestroy.restype = None
        lib.JxlThreadParallelRunnerDestroy.argtypes = [ctypes.c_void_p]
        lib.JxlThreadParallelRunnerDefaultNumWorkerThreads.restype = ctypes.c_size_t
        lib.JxlThreadParallelRunnerDefaultNumWorkerThreads.argtypes = []
        return lib
    # endregion

    def request_decode(self, num_bytes):
        with self._condition:
            self.target_bytes = num_bytes
            self._new_request = True
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._running = False
            self._condition.notify()

    # region thread lifecycle
    def run(self):
        if not self.lib:
            self.error_occurred.emit("libjxl not found on system.")
            return

        if self.threads_lib:
            n = self.threads_lib.JxlThreadParallelRunnerDefaultNumWorkerThreads()
            self.runner = self.threads_lib.JxlThreadParallelRunnerCreate(None, n)
            self._runner_fn = ctypes.cast(self.threads_lib.JxlThreadParallelRunner, ctypes.c_void_p)

        try:
            while True:
                with self._condition:
                    while not self._new_request and self._running:
                        self._condition.wait()
                    if not self._running:
                        break
                    self._new_request = False
                    current_target = self.target_bytes
                self._decode_to(current_target)
        finally:
            if self._dec:
                self.lib.JxlDecoderDestroy(self._dec)
                self._dec = None
            if self.runner:
                self.threads_lib.JxlThreadParallelRunnerDestroy(self.runner)
                self.runner = None
    # endregion

    # region decoding
    def _reset_decoder(self):
        """Destroy the current decoder and start a fresh one (used for backwards seeks)."""
        if self._dec:
            self.lib.JxlDecoderDestroy(self._dec)
        self._dec = self.lib.JxlDecoderCreate(None)
        if not self._dec:
            return False
        if self.runner:
            self.lib.JxlDecoderSetParallelRunner(self._dec, self._runner_fn, self.runner)

        events = self.JXL_DEC_BASIC_INFO | self.JXL_DEC_FULL_IMAGE | self.JXL_DEC_FRAME_PROGRESSION
        self.lib.JxlDecoderSubscribeEvents(self._dec, events)
        self.lib.JxlDecoderSetProgressiveDetail(self._dec, 3)

        self._fmt = self.JxlPixelFormat(4, self.JXL_TYPE_UINT8, self.JXL_NATIVE_ENDIAN, 0)
        self._pixels = None
        self._w = 0
        self._h = 0
        self._fed_bytes = 0
        return True

    def _emit_frame(self):
        if self._pixels:
            img = QImage(self._pixels.raw, self._w, self._h, QImage.Format_RGBA8888).copy()
            self.frame_ready.emit(img)

    def _decode_to(self, target):
        # Nothing to render at 0 bytes
        if target <= 0:
            if self._dec and self._fed_bytes > 0:
                self._reset_decoder()
            self.not_enough_data.emit()
            return

        if self._dec is None:
            if not self._reset_decoder():
                self.error_occurred.emit("Failed to create JXL decoder.")
                return

        # Backwards seek: a live decoder can't be rewound, so rebuild from byte 0
        if target < self._fed_bytes:
            if not self._reset_decoder():
                self.error_occurred.emit("Failed to create JXL decoder.")
                return

        # Nothing new to feed — leave the current frame on screen
        if target == self._fed_bytes:
            return

        # Forward: release the previous chunk, then feed only the new bytes
        if self._fed_bytes > 0:
            self.lib.JxlDecoderReleaseInput(self._dec)

        offset = self._fed_bytes
        count = target - offset
        ptr = ctypes.cast(
            ctypes.addressof(self._c_data) + offset,
            ctypes.POINTER(ctypes.c_uint8)
        )
        status = self.lib.JxlDecoderSetInput(self._dec, ptr, count)
        if status != self.JXL_DEC_SUCCESS:
            self.error_occurred.emit(f"JxlDecoderSetInput failed (status={status})")
            return
        self._fed_bytes = target

        self._process_loop()

    def _process_loop(self):
        # 2 KB is plenty for the truncated JxlBasicInfo we read
        basic_info_buf = ctypes.create_string_buffer(2048)

        while True:
            status = self.lib.JxlDecoderProcessInput(self._dec)

            if status == self.JXL_DEC_FRAME_PROGRESSION:
                # Target-based viewer: skip intermediate refinements, keep decoding
                continue

            elif status == self.JXL_DEC_BASIC_INFO:
                if self.lib.JxlDecoderGetBasicInfo(self._dec, basic_info_buf) == self.JXL_DEC_SUCCESS:
                    info = ctypes.cast(basic_info_buf, ctypes.POINTER(self.JxlBasicInfo)).contents
                    self._w = info.xsize
                    self._h = info.ysize
                continue

            elif status == self.JXL_DEC_NEED_IMAGE_OUT_BUFFER:
                buf_size = ctypes.c_size_t(0)
                if self.lib.JxlDecoderImageOutBufferSize(
                    self._dec, ctypes.byref(self._fmt), ctypes.byref(buf_size)
                ) == self.JXL_DEC_SUCCESS:
                    self._pixels = ctypes.create_string_buffer(buf_size.value)
                    self.lib.JxlDecoderSetImageOutBuffer(
                        self._dec, ctypes.byref(self._fmt), self._pixels, buf_size.value
                    )
                continue

            elif status == self.JXL_DEC_FULL_IMAGE:
                self._emit_frame()
                return

            elif status == self.JXL_DEC_NEED_MORE_INPUT:
                # Partial render: flush whatever is decodable so far
                if self._pixels:
                    if self.lib.JxlDecoderFlushImage(self._dec) == self.JXL_DEC_SUCCESS:
                        self._emit_frame()
                else:
                    self.not_enough_data.emit()
                return

            elif status == self.JXL_DEC_SUCCESS:
                return

            else:
                # JXL_DEC_ERROR or anything unexpected
                self.not_enough_data.emit()
                return
    # endregion


# ==========================================
# Custom QGraphicsView for Zooming / Panning
# ==========================================
class ImageGraphicsView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)

        self.pixmap_item = QGraphicsPixmapItem()
        self.scene.addItem(self.pixmap_item)

        self.text_item = self.scene.addText("")
        self.text_item.setDefaultTextColor(QColor("white"))
        font = QFont("Arial", 16, QFont.Bold)
        self.text_item.setFont(font)

        self.setRenderHint(QPainter.Antialiasing, True)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)

        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)

        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#1e1e1e"))

    def set_image(self, image):
        self.text_item.hide()
        self.pixmap_item.show()
        self.pixmap_item.setPixmap(QPixmap.fromImage(image))
        self.scene.setSceneRect(self.pixmap_item.boundingRect())

    def set_status(self, text):
        self.pixmap_item.hide()
        self.text_item.setPlainText(text)
        self.text_item.show()
        self.scene.setSceneRect(self.text_item.boundingRect())

    def wheelEvent(self, event):
        zoom_in_factor = 1.15
        zoom_out_factor = 1.0 / zoom_in_factor
        if event.angleDelta().y() > 0:
            self.scale(zoom_in_factor, zoom_in_factor)
        else:
            self.scale(zoom_out_factor, zoom_out_factor)


# ==========================================
# Main Window UI & Logic
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self, preferred_filename=None):
        super().__init__()
        self.setWindowTitle("Progressive JXL Stream Viewer")
        self.resize(1200, 768)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        controls_layout = QHBoxLayout()

        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(240)
        self.file_combo.currentTextChanged.connect(self.on_file_selected)

        self.info_label = QLabel("Loading...")
        self.info_label.setFixedWidth(210)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self.on_slider_moved)

        # --- Slow-load simulation ---
        self.sim_spin = QDoubleSpinBox()
        self.sim_spin.setRange(0.05, 3600.0)
        self.sim_spin.setDecimals(2)
        self.sim_spin.setSingleStep(0.5)
        self.sim_spin.setValue(3.0)
        self.sim_spin.setSuffix(" s")
        self.sim_spin.setFixedWidth(95)

        self.sim_button = QPushButton("Simulate Slow Load")
        self.sim_button.setFixedWidth(150)
        self.sim_button.clicked.connect(self.start_simulation)

        controls_layout.addWidget(self.file_combo)
        controls_layout.addWidget(self.info_label)
        controls_layout.addWidget(self.slider)
        controls_layout.addWidget(self.sim_spin)
        controls_layout.addWidget(self.sim_button)
        layout.addLayout(controls_layout)

        self.viewer = ImageGraphicsView()
        layout.addWidget(self.viewer)

        self.trailing_timer = QTimer()
        self.trailing_timer.setSingleShot(True)
        self.trailing_timer.timeout.connect(self.trigger_decode)

        self.sim_timer = QTimer()
        self.sim_timer.setInterval(16)
        self.sim_timer.timeout.connect(self._sim_tick)
        self._simulating = False
        self._sim_start = 0.0
        self._sim_duration = 0.0
        self._sim_total = 0

        self.file_data = b""
        self.worker = None
        self.target_bytes = 0

        self.last_update_time = 0.0
        self.throttle_interval = 0.032

        jxl_files = list_jxl_files(SCRIPT_DIR)

        self.file_combo.blockSignals(True)
        self.file_combo.addItems(jxl_files)
        self.file_combo.blockSignals(False)

        if jxl_files:
            preferred = os.path.basename(preferred_filename) if preferred_filename else None
            idx = jxl_files.index(preferred) if preferred in jxl_files else 0
            self.file_combo.blockSignals(True)
            self.file_combo.setCurrentIndex(idx)
            self.file_combo.blockSignals(False)
            self.switch_file(jxl_files[idx])
        else:
            self.viewer.set_status(f"No .jxl files found in:\n{SCRIPT_DIR}")

    # ---------------- file switching ----------------
    def on_file_selected(self, filename):
        if filename:
            self.switch_file(filename)

    def switch_file(self, filename):
        self.unload_file()
        self.load_file(os.path.join(SCRIPT_DIR, filename))

    def _cancel_simulation(self):
        if self._simulating:
            self.sim_timer.stop()
            self._simulating = False
        self.file_combo.setEnabled(True)
        self.sim_button.setEnabled(True)

    def unload_file(self):
        self._cancel_simulation()

        if self.worker:
            try:
                self.worker.frame_ready.disconnect(self.on_frame_ready)
                self.worker.not_enough_data.disconnect(self.on_not_enough_data)
                self.worker.error_occurred.disconnect(self.on_error)
            except (RuntimeError, TypeError):
                pass
            self.worker.stop()
            self.worker.wait()
            self.worker = None

        self.trailing_timer.stop()

        self.viewer.pixmap_item.hide()
        self.viewer.text_item.hide()
        self.viewer.scene.setSceneRect(0, 0, 1, 1)

        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.setMaximum(0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(False)

        self.file_data = b""
        self.target_bytes = 0
        self.last_update_time = 0.0

    def load_file(self, path):
        if not os.path.exists(path):
            self.viewer.set_status(f"File not found: {path}")
            return

        with open(path, "rb") as f:
            self.file_data = f.read()

        total_size = len(self.file_data)

        # Block signals so setValue doesn't call on_slider_moved before worker exists
        self.slider.blockSignals(True)
        self.slider.setMaximum(total_size)
        self.slider.setValue(total_size)
        self.slider.blockSignals(False)
        self.slider.setEnabled(True)

        kb = total_size / 1024
        self.info_label.setText(f"{kb:.1f} / {kb:.1f} KB (100.0%)")

        self.target_bytes = total_size
        self.last_update_time = 0.0

        self.worker = JxlWorker(self.file_data)
        self.worker.frame_ready.connect(self.on_frame_ready)
        self.worker.not_enough_data.connect(self.on_not_enough_data)
        self.worker.error_occurred.connect(self.on_error)
        self.worker.start()

    # ---------------- manual slider interaction ----------------
    def on_slider_moved(self, value):
        self.target_bytes = value
        kb_val = self.target_bytes / 1024
        total_kb = len(self.file_data) / 1024
        percentage = (self.target_bytes / len(self.file_data)) * 100
        self.info_label.setText(f"{kb_val:.1f} / {total_kb:.1f} KB ({percentage:.1f}%)")

        current_time = time.perf_counter()
        if current_time - self.last_update_time >= self.throttle_interval:
            self.last_update_time = current_time
            self.trigger_decode()
        else:
            self.trailing_timer.start(50)

    # ---------------- slow-load simulation ----------------
    def start_simulation(self):
        if not self.file_data or not self.worker:
            return

        if self._simulating:
            self.sim_timer.stop()
            self._simulating = False

        self._sim_duration = max(0.05, self.sim_spin.value())
        self._sim_total = len(self.file_data)
        self._sim_start = time.perf_counter()
        self._simulating = True

        self.trailing_timer.stop()
        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(False)        # slider becomes a progress bar
        self.file_combo.setEnabled(False)
        self.sim_button.setEnabled(False)

        self.target_bytes = 0
        self.last_update_time = 0.0

        total_kb = self._sim_total / 1024
        self.info_label.setText(f"0.0 / {total_kb:.1f} KB (0.0%)")

        self.trigger_decode()
        self.sim_timer.start()

    def _sim_tick(self):
        elapsed = time.perf_counter() - self._sim_start
        progress = min(1.0, elapsed / self._sim_duration)
        target = int(progress * self._sim_total)

        self.slider.blockSignals(True)
        self.slider.setValue(target)
        self.slider.blockSignals(False)

        total_kb = self._sim_total / 1024
        self.info_label.setText(f"{target / 1024:.1f} / {total_kb:.1f} KB ({progress * 100:.1f}%)")

        now = time.perf_counter()
        if now - self.last_update_time >= self.throttle_interval:
            self.last_update_time = now
            self.target_bytes = target
            self.trigger_decode()

        if progress >= 1.0:
            self._finish_simulation()

    def _finish_simulation(self):
        self.sim_timer.stop()
        self.trailing_timer.stop()
        self._simulating = False
        self.slider.setEnabled(True)
        self.file_combo.setEnabled(True)
        self.sim_button.setEnabled(True)

        self.target_bytes = self._sim_total
        self.last_update_time = 0.0
        self.trigger_decode()

    # ---------------- decode plumbing ----------------
    def trigger_decode(self):
        if self.worker:
            self.worker.request_decode(self.target_bytes)

    def on_frame_ready(self, img):
        self.viewer.set_image(img)

    def on_not_enough_data(self):
        self.viewer.set_status("⚠️ Not enough bytes to render an image block yet...")

    def on_error(self, err_msg):
        self.viewer.set_status(f"Error: {err_msg}")

    def closeEvent(self, event):
        self.sim_timer.stop()
        if self.worker:
            self.worker.stop()
            self.worker.wait()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    preferred = sys.argv[1] if len(sys.argv) > 1 else None
    window = MainWindow(preferred)
    window.show()
    sys.exit(app.exec())
