"""trt_ssd_async.py

This is the 'async' version of trt_ssd.py implementation.  It creates
1 dedicated child thread for fetching camera input and do inferencing
with the TensorRT optimized SSD model/engine, while using the main
thread for drawing detection results and displaying video.  Ideally,
the 2 threads work in a pipeline fashion so overall throughput (FPS)
would be improved comparing to the non-async version.
"""


import sys
import time
import ctypes
import argparse
import threading

import numpy as np
import cv2
import pycuda.driver as cuda
import tensorrt as trt

from utils.ssd_classes import get_cls_dict
from utils.camera import add_camera_args, Camera
from utils.display import open_window, set_display, show_fps
from utils.visualization import BBoxVisualization


WINDOW_NAME = 'TrtSsdDemoAsync'
INPUT_WH = (300, 300)
OUTPUT_LAYOUT = 7
SUPPORTED_MODELS = [
    'ssd_mobilenet_v1_coco',
    'ssd_mobilenet_v1_egohands',
    'ssd_mobilenet_v2_coco',
    'ssd_mobilenet_v2_egohands',
]

# These global variables are 'shared' between the main and child
# threads.  The child thread writes new frame and detection result
# into these variables, while the main thread reads from them.
s_img, s_boxes, s_confs, s_clss = None, None, None, None


def parse_args():
    """Parse input arguments."""
    desc = ('Capture and display live camera video, while doing '
            'real-time object detection with TensorRT optimized '
            'SSD model on Jetson Nano')
    parser = argparse.ArgumentParser(description=desc)
    parser = add_camera_args(parser)
    parser.add_argument('--model', type=str, default='ssd_mobilenet_v2_coco',
                        choices=SUPPORTED_MODELS)
    args = parser.parse_args()
    return args


def preprocess(img):
    """Preprocess an image before SSD inferencing."""
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = cv2.resize(img, INPUT_WH)
    img = img.transpose((2, 0, 1)).astype(np.float32)
    img = (2.0/255.0) * img - 1.0
    return img


def postprocess(img, output, conf_th):
    """Postprocess TRT SSD output."""
    img_h, img_w, _ = img.shape
    boxes, confs, clss = [], [], []
    for prefix in range(0, len(output), OUTPUT_LAYOUT):
        #index = int(output[prefix+0])
        conf = float(output[prefix+2])
        if conf < conf_th:
            continue
        x1 = int(output[prefix+3] * img_w)
        y1 = int(output[prefix+4] * img_h)
        x2 = int(output[prefix+5] * img_w)
        y2 = int(output[prefix+6] * img_h)
        cls = int(output[prefix+1])
        boxes.append((x1, y1, x2, y2))
        confs.append(conf)
        clss.append(cls)
    return boxes, confs, clss


class TrtSSD(object):
    """TrtSSD class encapsulates things needed to run TRT SSD."""

    def _load_plugins(self):
        ctypes.CDLL("ssd/libflattenconcat.so")
        trt.init_libnvinfer_plugins(self.trt_logger, '')

    def _load_engine(self):
        TRTbin = 'ssd/TRT_%s.bin' % self.model
        with open(TRTbin, 'rb') as f, trt.Runtime(self.trt_logger) as runtime:
            return runtime.deserialize_cuda_engine(f.read())

    def _create_context(self):
        for binding in self.engine:
            size = trt.volume(self.engine.get_binding_shape(binding)) * \
                   self.engine.max_batch_size
            host_mem = cuda.pagelocked_empty(size, np.float32)
            cuda_mem = cuda.mem_alloc(host_mem.nbytes)
            self.bindings.append(int(cuda_mem))
            if self.engine.binding_is_input(binding):
                self.host_inputs.append(host_mem)
                self.cuda_inputs.append(cuda_mem)
            else:
                self.host_outputs.append(host_mem)
                self.cuda_outputs.append(cuda_mem)
        return self.engine.create_execution_context()

    def __init__(self, model):
        """Initialize TensorRT plugins, engine and conetxt."""
        self.model = model
        self.trt_logger = trt.Logger(trt.Logger.INFO)
        self._load_plugins()
        self.engine = self._load_engine()

        self.host_inputs = []
        self.cuda_inputs = []
        self.host_outputs = []
        self.cuda_outputs = []
        self.bindings = []
        self.stream = cuda.Stream()
        self.context = self._create_context()

    def __del__(self):
        """Free CUDA memories."""
        del self.stream
        del self.cuda_outputs
        del self.cuda_inputs

    def detect(self, img, conf_th=0.3):
        """Detect objects in the input image."""
        img_resized = preprocess(img)
        np.copyto(self.host_inputs[0], img_resized.ravel())

        cuda.memcpy_htod_async(
            self.cuda_inputs[0], self.host_inputs[0], self.stream)
        self.context.execute_async(
            batch_size=1,
            bindings=self.bindings,
            stream_handle=self.stream.handle)
        cuda.memcpy_dtoh_async(
            self.host_outputs[1], self.cuda_outputs[1], self.stream)
        cuda.memcpy_dtoh_async(
            self.host_outputs[0], self.cuda_outputs[0], self.stream)
        self.stream.synchronize()

        output = self.host_outputs[0]
        return postprocess(img, output, conf_th)


class TrtThread(threading.Thread):
    """TrtThread

    This implements the child thread which continues to read images
    from cam (input) and to do TRT engine inferencing.  The child
    thread stores the input image and detection results into global
    variables and uses a condition varaiable to inform main thread.
    In other words, the TrtThread acts as the producer while the
    main thread is the consumer.
    """
    def __init__(self, condition, cam, model, conf_th):
        """__init__

        # Arguments
            condition: the condition variable used to notify main
                       thread about new frame and detection result
            cam: the camera object for reading input image frames
            model: a string, specifying the TRT SSD model
            conf_th: confidence threshold for detection
        """
        threading.Thread.__init__(self)
        self.condition = condition
        self.cam = cam
        self.model = model
        self.conf_th = conf_th
        self.cuda_ctx = None  # to be created when run
        self.trt_ssd = None   # to be created when run
        self.running = False

    def run(self):
        """Run until 'running' flag is set to False by main thread.

        NOTE: CUDA context is created here (NOTE: inside the thread
        which would calls CUDA kernelis, instead of the main thread).
        """
        global s_img, s_boxes, s_confs, s_clss

        print('TrtThread: loading the TRT SSD engine...')
        self.cuda_ctx = cuda.Device(0).make_context()  # GPU 0
        self.trt_ssd = TrtSSD(self.model)
        print('TrtThread: start running...')
        self.running = True
        while self.running:
            img = self.cam.read()
            boxes, confs, clss = self.trt_ssd.detect(img, self.conf_th)
            with self.condition:
                s_img, s_boxes, s_confs, s_clss = img, boxes, confs, clss
                self.condition.notify()
        del self.trt_ssd
        self.cuda_ctx.pop()
        del self.cuda_ctx
        print('TrtThread: stopped...')

    def stop(self):
        self.running = False
        self.join()


def loop_and_display(condition, vis):
    """Take detection results from the child thread and display.

    # Arguments
        condition: the condition variable for synchronization with
                   the child thread.
        vis: for visualization.
    """
    global s_img, s_boxes, s_confs, s_clss

    full_scrn = False
    fps = 0.0
    tic = time.time()
    while True:
        if cv2.getWindowProperty(WINDOW_NAME, 0) < 0:
            break
        with condition:
            # Wait for the next frame and detection result.  When
            # getting the signal from the child thread, save the
            # references to the frame and detection result for
            # display.
            condition.wait()
            img, boxes, confs, clss = s_img, s_boxes, s_confs, s_clss
        img = vis.draw_bboxes(img, boxes, confs, clss)
        img = show_fps(img, fps)
        cv2.imshow(WINDOW_NAME, img)
        toc = time.time()
        curr_fps = 1.0 / (toc - tic)
        # calculate an exponentially decaying average of fps number
        fps = curr_fps if fps == 0.0 else (fps*0.95 + curr_fps*0.05)
        tic = toc
        key = cv2.waitKey(1)
        if key == 27:  # ESC key: quit program
            break
        elif key == ord('F') or key == ord('f'):  # Toggle fullscreen
            full_scrn = not full_scrn
            set_display(WINDOW_NAME, full_scrn)


def main():
    args = parse_args()
    cam = Camera(args)
    cam.open()
    if not cam.is_opened:
        sys.exit('Failed to open camera!')

    cls_dict = get_cls_dict(args.model.split('_')[-1])

    cuda.init()  # init pycuda driver

    cam.start()  # let camera start grabbing frames
    open_window(WINDOW_NAME, args.image_width, args.image_height,
                'Camera TensorRT SSD Demo for Jetson Nano')
    vis = BBoxVisualization(cls_dict)
    condition = threading.Condition()
    trt_thread = TrtThread(condition, cam, args.model, conf_th=0.3)
    trt_thread.start()  # start the child thread
    loop_and_display(condition, vis)
    trt_thread.stop()   # stop the child thread

    cam.stop()
    cam.release()
    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
