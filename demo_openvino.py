import argparse
import os
import pprint
import sys

sys.path.append('C:\Program Files (x86)\IntelSWTools\openvino_2019.1.087\python\python3.6')

from openvino.inference_engine import IENetwork, IEPlugin
import torch
import torchvision.transforms as transforms
import torch.nn as nn
import matplotlib.pyplot as plt
import cv2
import numpy as np


#model
DECONV_WITH_BIAS = False
NUM_DECONV_LAYERS = 3
NUM_DECONV_FILTERS = [256, 256, 256]
NUM_DECONV_KERNELS = [4, 4, 4]
FINAL_CONV_KERNEL = 1
NUM_JOINTS = 16
BN_MOMENTUM = 0.1
IMAGE_SIZE = [256, 256]

image = np.empty(())

def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.conv3 = nn.Conv2d(planes, planes * self.expansion, kernel_size=1,
                               bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion,
                                  momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class PoseResNet(nn.Module):

    def __init__(self, block, layers, **kwargs):
        self.inplanes = 64
        self.deconv_with_bias = DECONV_WITH_BIAS

        super(PoseResNet, self).__init__()
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = nn.BatchNorm2d(64, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)

        # used for deconv layers
        self.deconv_layers = self._make_deconv_layer(
            NUM_DECONV_LAYERS,
            NUM_DECONV_FILTERS,
            NUM_DECONV_KERNELS,
        )

        self.final_layer = nn.Conv2d(
            in_channels=NUM_DECONV_FILTERS[-1],
            out_channels=NUM_JOINTS,
            kernel_size=FINAL_CONV_KERNEL,
            stride=1,
            padding=1 if FINAL_CONV_KERNEL == 3 else 0
        )

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion, momentum=BN_MOMENTUM),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def _get_deconv_cfg(self, deconv_kernel, index):
        if deconv_kernel == 4:
            padding = 1
            output_padding = 0
        elif deconv_kernel == 3:
            padding = 1
            output_padding = 1
        elif deconv_kernel == 2:
            padding = 0
            output_padding = 0

        return deconv_kernel, padding, output_padding

    def _make_deconv_layer(self, num_layers, num_filters, num_kernels):
        assert num_layers == len(num_filters), \
            'ERROR: num_deconv_layers is different len(num_deconv_filters)'
        assert num_layers == len(num_kernels), \
            'ERROR: num_deconv_layers is different len(num_deconv_filters)'

        layers = []
        for i in range(num_layers):
            kernel, padding, output_padding = \
                self._get_deconv_cfg(num_kernels[i], i)

            planes = num_filters[i]
            layers.append(
                nn.ConvTranspose2d(
                    in_channels=self.inplanes,
                    out_channels=planes,
                    kernel_size=kernel,
                    stride=2,
                    padding=padding,
                    output_padding=output_padding,
                    bias=self.deconv_with_bias))
            layers.append(nn.BatchNorm2d(planes, momentum=BN_MOMENTUM))
            layers.append(nn.ReLU(inplace=True))
            self.inplanes = planes

        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.deconv_layers(x)
        x = self.final_layer(x)

        return x


                    

# predication
def get_max_preds(batch_heatmaps):
    '''
    get predictions from score maps
    heatmaps: numpy.ndarray([batch_size, num_joints, height, width])
    '''
    assert isinstance(batch_heatmaps, np.ndarray), \
        'batch_heatmaps should be numpy.ndarray'
    assert batch_heatmaps.ndim == 4, 'batch_images should be 4-ndim'

    batch_size = batch_heatmaps.shape[0]
    num_joints = batch_heatmaps.shape[1]
    width = batch_heatmaps.shape[3]
    heatmaps_reshaped = batch_heatmaps.reshape((batch_size, num_joints, -1))
    idx = np.argmax(heatmaps_reshaped, 2)
    maxvals = np.amax(heatmaps_reshaped, 2)

    maxvals = maxvals.reshape((batch_size, num_joints, 1))
    idx = idx.reshape((batch_size, num_joints, 1))

    preds = np.tile(idx, (1, 1, 2)).astype(np.float32)

    preds[:, :, 0] = (preds[:, :, 0]) % width
    preds[:, :, 1] = np.floor((preds[:, :, 1]) / width)

    pred_mask = np.tile(np.greater(maxvals, 0.0), (1, 1, 2))
    pred_mask = pred_mask.astype(np.float32)

    preds *= pred_mask
    return preds, maxvals
 
 # main and args
def parse_args():
    parser = argparse.ArgumentParser(description='Train keypoints network')
    
    parser.add_argument('--model-file',
                        help='Path to an .xml file with a trained mode',
                        required=True,
                        type=str)
    parser.add_argument('--image-file',
                        help='image for predication',
                        # required=True,
                        type=str)
    parser.add_argument('--save-transform-image',
                        help='Save temp image after transforms (True/False)',
                        action='store_true')
    parser.add_argument('--use-webcam',
                        help='Use webcam for predication',
                        action='store_true')
    parser.add_argument('--use-crop-mode',
                        help='Use crop mode for cropping person, that are you want to predict',
                        action='store_true')
    parser.add_argument('--gpu',
                        help='Use GPU',
                        action='store_true')
    parser.add_argument('--min-confidence-threshold',
                        help='Minimum confidence threshold for drawing. Default: 0.5',
                        type=str)
    args = parser.parse_args()

    return args

refPt = []
cropping = False
tempPosition = ()

def click_and_crop(event, x, y, flags, param):
	# grab references to the global variables
    global refPt, cropping, tempPosition
 
	# if the left mouse button was clicked, record the starting
	# (x, y) coordinates and indicate that cropping is being
	# performed
    if event == cv2.EVENT_LBUTTONDOWN:
        refPt = [(x, y)]
        cropping = True
 
	# check to see if the left mouse button was released
    elif event == cv2.EVENT_LBUTTONUP:
		# record the ending (x, y) coordinates and indicate that
		# the cropping operation is finished
        refPt.append((x, y))
        cropping = False
    else:
        tempPosition = (x, y)
        
def main():
    global refPt, tempPosition
    args = parse_args()
    
    transform_image = False
    use_webcam = False
    gpu = False
    use_crop = False
    min_confidence_threshold = 0.5
    
    if args.model_file:
        model_xml = args.model_file
        model_bin = os.path.splitext(model_xml)[0] + ".bin"
    if args.image_file:
        image_file = args.image_file   
    if args.save_transform_image:
        transform_image = args.save_transform_image
    if args.use_webcam:
        use_webcam = args.use_webcam
    if args.gpu:
        gpu = args.gpu
    if args.use_crop_mode:
        use_crop = args.use_crop_mode
    if args.min_confidence_threshold:
        min_confidence_threshold = np.float(args.min_confidence_threshold)
        

    if model_xml:
        print("Loading network files:\n\t{}\n\t{}".format(model_xml, model_bin))
        net = IENetwork(model=model_xml, weights=model_bin)
        net.batch_size = 1
    else:
        print('Error')
        return
    if gpu == True:
        plugin = IEPlugin('GPU')
    else:
        plugin = IEPlugin('CPU')
    
    # if plugin.device == "CPU":
        # supported_layers = plugin.get_supported_layers(net)
        # not_supported_layers = [l for l in net.layers.keys() if l not in supported_layers]
        # if len(not_supported_layers) != 0:
            # log.error("Following layers are not supported by the plugin for specified device {}:\n {}".
                      # format(plugin.device, ', '.join(not_supported_layers)))
            # log.error("Please try to specify cpu extensions library path in sample's command line parameters using -l "
                      # "or --cpu_extension command line argument")
            # sys.exit(1)
        # assert len(net.inputs.keys()) == 1, "Sample supports only single input topologies"
        # assert len(net.outputs) == 1, "Sample supports only single output topologies"
    
    input_blob = next(iter(net.inputs))
    print(net.inputs['input_1'].shape)
    print("Loading model to the plugin")
    exec_net = plugin.load(network=net)
    print("Loaded")
    IMAGE_SIZE[0] = net.inputs['input_1'].shape[2]
    IMAGE_SIZE[1] = net.inputs['input_1'].shape[3]
    del net
    
    if use_webcam == False:
        ## Load an image
        data_numpy = cv2.imread(image_file, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if data_numpy is None:
            raise ValueError('Fail to read image {}'.format(image_file))
        print(data_numpy.shape)
        
        if use_crop == True:
            cv2.namedWindow("image")
            cv2.setMouseCallback("image", click_and_crop)
            
            while True:
                key = cv2.waitKey(1) & 0xFF
             
                if len(refPt) == 2:
                    temp = data_numpy.copy()
                    cv2.rectangle(temp, refPt[0], refPt[1], (0, 255, 0), 2)
                    cv2.imshow("image", temp)
                    cv2.waitKey(1) & 0xFF
                    break
                elif len(refPt) == 1:
                    temp = data_numpy.copy()
                    cv2.rectangle(temp, refPt[0], tempPosition, (0, 255, 0), 2)
                    cv2.imshow("image", temp)
                else:
                    cv2.imshow("image", data_numpy)
                    
            data_numpy = data_numpy[refPt[0][1]:refPt[1][1], refPt[0][0]:refPt[1][0]]
            
        input = cv2.resize(data_numpy, (IMAGE_SIZE[0], IMAGE_SIZE[1]))

        # vis transformed image
        if transform_image == True:
            copyInput = input.copy()
            cv2.rectangle(copyInput, (np.int(IMAGE_SIZE[0]/2 + IMAGE_SIZE[0]/4), np.int(IMAGE_SIZE[1]/2 + IMAGE_SIZE[1]/4)), 
                                     (np.int(IMAGE_SIZE[0]/2 - IMAGE_SIZE[0]/4), np.int(IMAGE_SIZE[1]/2 - IMAGE_SIZE[1]/4)), (255,0,0), 2)
            cv2.imwrite('transformed.jpg', copyInput)

        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225]),
            ])
        input = transform(input).unsqueeze(0)
        
        # switch to evaluate mode


        # compute output heatmap
        output = exec_net.infer(inputs={input_blob: input})['output1']
        coords, maxvals = get_max_preds(output)
        print(maxvals)
        cv2.waitKey(1000) & 0xFF
        image = data_numpy.copy()
        for i in range(coords[0].shape[0]):
            mat = coords[0,i]
            x, y = int(mat[0]), int(mat[1])
            if maxvals[0, i] >= min_confidence_threshold:
                cv2.circle(image, (np.int(x*data_numpy.shape[1]/output.shape[3]), 
                      np.int(y*data_numpy.shape[0]/output.shape[2])), 2, (0, 0, 255), 2)
               
        cv2.imwrite('result.jpg', image)
        cv2.imshow('result.jpg', image)
        cv2.waitKey(2000) & 0xFF
    
        print('Success')
    else:
        sample = cv2.imread('sample.png', -1)
        alpha_s = sample[:, :, 3] / 255.0
        alpha_l = 1.0 - alpha_s
        cap = cv2.VideoCapture(0)
        while(True):
            ret, data_numpy = cap.read()
            if not ret: break
                            
            input = cv2.resize(data_numpy, (IMAGE_SIZE[0], IMAGE_SIZE[1]))

            # vis transformed image
            if transform_image == True:
                copyInput = input.copy()
                cv2.rectangle(copyInput, (np.int(IMAGE_SIZE[0]/2 + IMAGE_SIZE[0]/4), np.int(IMAGE_SIZE[1]/2 + IMAGE_SIZE[1]/4)), 
                                         (np.int(IMAGE_SIZE[0]/2 - IMAGE_SIZE[0]/4), np.int(IMAGE_SIZE[1]/2 - IMAGE_SIZE[1]/4)), (255,0,0), 2)
                cv2.imwrite('transformed.jpg', copyInput)

            transform = transforms.Compose([
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                    std=[0.229, 0.224, 0.225]),
                ])
            input = transform(input).unsqueeze(0)
            
            # compute output heatmap
            output = exec_net.infer(inputs={input_blob: input})['output1']
            coords, maxvals = get_max_preds(output)
            image = data_numpy.copy()
            badPoints = 0
            for i in range(coords[0].shape[0]):
                mat = coords[0,i]
                x, y = int(mat[0]), int(mat[1])
                if maxvals[0, i] >= min_confidence_threshold:
                    cv2.circle(image, (np.int(x*data_numpy.shape[1]/output.shape[3]), 
                          np.int(y*data_numpy.shape[0]/output.shape[2])), 2, (0, 0, 255), 2)
                if maxvals[0, i] <= 0.4:
                    badPoints += 1
            if badPoints >= coords[0].shape[0]/3:
                cv2.rectangle(image, (np.int(data_numpy.shape[1]/2 + data_numpy.shape[1]/4), np.int(data_numpy.shape[0]/2 + data_numpy.shape[0]/4)), 
                                     (np.int(data_numpy.shape[1]/2 - data_numpy.shape[1]/4), np.int(data_numpy.shape[0]/2 - data_numpy.shape[0]/4)), (255,0,0), 2)
                for c in range(0, 3):
                    image[10:10+sample.shape[0], 10:10+sample.shape[1], c] = (alpha_s * sample[:, :, c] +
                              alpha_l * image[10:10+sample.shape[0], 10:10+sample.shape[1], c])
            cv2.imshow('result', image)
        
            cv2.waitKey(10)
        #if cv2.waitKey(1) & 0xFF == ord('q'): break

        cv2.release()
        cv2.destroyAllWindows()

if __name__ == '__main__':
    main()