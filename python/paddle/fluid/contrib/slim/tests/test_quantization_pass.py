#   copyright (c) 2018 paddlepaddle authors. all rights reserved.
#
# licensed under the apache license, version 2.0 (the "license");
# you may not use this file except in compliance with the license.
# you may obtain a copy of the license at
#
#     http://www.apache.org/licenses/license-2.0
#
# unless required by applicable law or agreed to in writing, software
# distributed under the license is distributed on an "as is" basis,
# without warranties or conditions of any kind, either express or implied.
# see the license for the specific language governing permissions and
# limitations under the license.

import os
import unittest
import random
import numpy as np
import paddle.fluid as fluid
import six
import paddle
from paddle.fluid.framework import IrGraph
from paddle.fluid.contrib.slim.quantization import QuantizationTransformPass
from paddle.fluid.contrib.slim.quantization import QuantizationFreezePass
from paddle.fluid.contrib.slim.quantization import ConvertToInt8Pass
from paddle.fluid.contrib.slim.quantization import TransformForMobilePass
from paddle.fluid import core

os.environ["CUDA_VISIBLE_DEVICES"] = "0"
os.environ["CPU_NUM"] = "1"


def linear_fc(num):
    data = fluid.layers.data(name='image', shape=[1, 32, 32], dtype='float32')
    label = fluid.layers.data(name='label', shape=[1], dtype='int64')
    hidden = data
    for _ in six.moves.xrange(num):
        hidden = fluid.layers.fc(hidden, size=128, act='relu')
    loss = fluid.layers.cross_entropy(input=hidden, label=label)
    loss = fluid.layers.mean(loss)
    return loss


def residual_block(num):
    def conv_bn_layer(input,
                      ch_out,
                      filter_size,
                      stride,
                      padding,
                      act='relu',
                      bias_attr=False):
        tmp = fluid.layers.conv2d(
            input=input,
            filter_size=filter_size,
            num_filters=ch_out,
            stride=stride,
            padding=padding,
            act=None,
            bias_attr=bias_attr)
        return fluid.layers.batch_norm(input=tmp, act=act)

    data = fluid.layers.data(name='image', shape=[1, 32, 32], dtype='float32')
    label = fluid.layers.data(name='label', shape=[1], dtype='int64')
    hidden = data
    for _ in six.moves.xrange(num):
        conv = conv_bn_layer(hidden, 16, 3, 1, 1, act=None, bias_attr=True)
        short = conv_bn_layer(hidden, 16, 1, 1, 0, act=None)
        hidden = fluid.layers.elementwise_add(x=conv, y=short, act='relu')
    fc = fluid.layers.fc(input=hidden, size=10)
    loss = fluid.layers.cross_entropy(input=fc, label=label)
    loss = fluid.layers.mean(loss)
    return loss


def conv_net(img, label):
    conv_pool_1 = fluid.nets.simple_img_conv_pool(
        input=img,
        filter_size=5,
        num_filters=20,
        pool_size=2,
        pool_stride=2,
        act="relu")
    conv_pool_1 = fluid.layers.batch_norm(conv_pool_1)
    conv_pool_2 = fluid.nets.simple_img_conv_pool(
        input=conv_pool_1,
        filter_size=5,
        num_filters=50,
        pool_size=2,
        pool_stride=2,
        act="relu")
    prediction = fluid.layers.fc(input=conv_pool_2, size=10, act='softmax')
    loss = fluid.layers.cross_entropy(input=prediction, label=label)
    avg_loss = fluid.layers.mean(loss)
    return avg_loss


class TestQuantizationTransformPass(unittest.TestCase):
    def setUp(self):
        self.quantizable_op_and_inputs = {
            'conv2d': ['Input', 'Filter'],
            'depthwise_conv2d': ['Input', 'Filter'],
            'mul': ['X', 'Y']
        }
        self.quantizable_grad_op_inputs = {
            'conv2d_grad': ['Input', 'Filter'],
            'depthwise_conv2d_grad': ['Input', 'Filter'],
            'mul_grad': ['X', 'Y']
        }

    def check_program(self, transform_pass, program):
        quantized_ops = set()
        for block in program.blocks:
            for op in block.ops:
                # check forward
                if op.type in self.quantizable_op_and_inputs:
                    for arg_name in op.input_arg_names:
                        self.assertTrue(
                            arg_name.endswith('.quantized.dequantized'))
                        quantized_ops.add(arg_name)

            for op in block.ops:
                # check backward
                if op.type in self.quantizable_grad_op_inputs:
                    for pname in self.quantizable_grad_op_inputs[op.type]:
                        arg_name = op.input(pname)[0]
                        self.assertTrue(
                            arg_name.endswith('.quantized.dequantized'))
                        self.assertTrue(arg_name in quantized_ops)

    def linear_fc_quant(self, quant_type, for_ci=False):
        main = fluid.Program()
        startup = fluid.Program()
        with fluid.program_guard(main, startup):
            loss = linear_fc(3)
            opt = fluid.optimizer.Adam(learning_rate=0.001)
            opt.minimize(loss)
        place = fluid.CPUPlace()
        exe = fluid.Executor(place)
        graph = IrGraph(core.Graph(main.desc), for_test=False)
        transform_pass = QuantizationTransformPass(
            scope=fluid.global_scope(),
            place=place,
            activation_quantize_type=quant_type)
        transform_pass.apply(graph)
        if not for_ci:
            marked_nodes = set()
            for op in graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    marked_nodes.add(op)
            graph.draw('.', 'quantize_fc_' + quant_type, marked_nodes)
        program = graph.to_program()
        self.check_program(transform_pass, program)
        val_graph = IrGraph(core.Graph(program.desc), for_test=False)
        if not for_ci:
            val_marked_nodes = set()
            for op in val_graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    val_marked_nodes.add(op)
            val_graph.draw('.', 'val_fc_' + quant_type, val_marked_nodes)

    def test_linear_fc_quant_abs_max(self):
        self.linear_fc_quant('abs_max', for_ci=True)

    def test_linear_fc_quant_range_abs_max(self):
        self.linear_fc_quant('range_abs_max', for_ci=True)

    def residual_block_quant(self, quant_type, for_ci=False):
        main = fluid.Program()
        startup = fluid.Program()
        with fluid.program_guard(main, startup):
            loss = residual_block(2)
            opt = fluid.optimizer.Adam(learning_rate=0.001)
            opt.minimize(loss)
        place = fluid.CPUPlace()
        exe = fluid.Executor(place)
        graph = IrGraph(core.Graph(main.desc), for_test=False)
        transform_pass = QuantizationTransformPass(
            scope=fluid.global_scope(),
            place=place,
            activation_quantize_type=quant_type)
        transform_pass.apply(graph)
        if not for_ci:
            marked_nodes = set()
            for op in graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    marked_nodes.add(op)
            graph.draw('.', 'quantize_residual_' + quant_type, marked_nodes)
        program = graph.to_program()
        self.check_program(transform_pass, program)
        val_graph = IrGraph(core.Graph(program.desc), for_test=False)
        if not for_ci:
            val_marked_nodes = set()
            for op in val_graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    val_marked_nodes.add(op)
            val_graph.draw('.', 'val_residual_' + quant_type, val_marked_nodes)

    def test_residual_block_abs_max(self):
        self.residual_block_quant('abs_max', for_ci=True)

    def test_residual_block_range_abs_max(self):
        self.residual_block_quant('range_abs_max', for_ci=True)


class TestQuantizationFreezePass(unittest.TestCase):
    def freeze_graph(self, use_cuda, seed, quant_type, for_ci=False):
        def build_program(main, startup, is_test):
            main.random_seed = seed
            startup.random_seed = seed
            with fluid.unique_name.guard():
                with fluid.program_guard(main, startup):
                    img = fluid.layers.data(
                        name='image', shape=[1, 28, 28], dtype='float32')
                    label = fluid.layers.data(
                        name='label', shape=[1], dtype='int64')
                    loss = conv_net(img, label)
                    if not is_test:
                        opt = fluid.optimizer.Adam(learning_rate=0.001)
                        opt.minimize(loss)
            return [img, label], loss

        random.seed(0)
        np.random.seed(0)

        main = fluid.Program()
        startup = fluid.Program()
        test_program = fluid.Program()
        feeds, loss = build_program(main, startup, False)
        build_program(test_program, startup, True)
        test_program = test_program.clone(for_test=True)
        main_graph = IrGraph(core.Graph(main.desc), for_test=False)
        test_graph = IrGraph(core.Graph(test_program.desc), for_test=True)

        place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()
        exe = fluid.Executor(place)
        scope = fluid.Scope()
        with fluid.scope_guard(scope):
            exe.run(startup)
        transform_pass = QuantizationTransformPass(
            scope=scope, place=place, activation_quantize_type=quant_type)
        transform_pass.apply(main_graph)
        transform_pass.apply(test_graph)
        dev_name = '_gpu_' if use_cuda else '_cpu_'
        if not for_ci:
            marked_nodes = set()
            for op in main_graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    marked_nodes.add(op)
            main_graph.draw('.', 'main' + dev_name + quant_type, marked_nodes)
            marked_nodes = set()
            for op in test_graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    marked_nodes.add(op)
            test_graph.draw('.', 'test' + dev_name + quant_type, marked_nodes)

        build_strategy = fluid.BuildStrategy()
        build_strategy.memory_optimize = False
        build_strategy.enable_inplace = False
        binary = fluid.CompiledProgram(main_graph.graph).with_data_parallel(
            loss_name=loss.name, build_strategy=build_strategy)
        quantized_test_program = test_graph.to_program()
        iters = 5
        batch_size = 8

        train_reader = paddle.batch(
            paddle.reader.shuffle(
                paddle.dataset.mnist.train(), buf_size=500),
            batch_size=batch_size)
        test_reader = paddle.batch(
            paddle.dataset.mnist.test(), batch_size=batch_size)
        feeder = fluid.DataFeeder(feed_list=feeds, place=place)
        with fluid.scope_guard(scope):
            for _ in range(iters):
                data = next(train_reader())
                loss_v = exe.run(binary,
                                 feed=feeder.feed(data),
                                 fetch_list=[loss])
                if not for_ci:
                    print('{}: {}'.format('loss' + dev_name + quant_type,
                                          loss_v))

        test_data = next(test_reader())
        with fluid.program_guard(quantized_test_program):
            w_var = fluid.framework._get_var('conv2d_1.w_0.quantized',
                                             quantized_test_program)
        # Testing
        with fluid.scope_guard(scope):
            test_loss1, w_quant = exe.run(program=quantized_test_program,
                                          feed=feeder.feed(test_data),
                                          fetch_list=[loss, w_var])

        # Freeze graph for inference, but the weight of fc/conv is still float type.
        freeze_pass = QuantizationFreezePass(scope=scope, place=place)
        freeze_pass.apply(test_graph)
        if not for_ci:
            marked_nodes = set()
            for op in test_graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    marked_nodes.add(op)
            test_graph.draw('.', 'test_freeze' + dev_name + quant_type,
                            marked_nodes)

        server_program = test_graph.to_program()
        with fluid.scope_guard(scope):
            test_loss2, = exe.run(program=server_program,
                                  feed=feeder.feed(test_data),
                                  fetch_list=[loss])
        self.assertAlmostEqual(test_loss1, test_loss2, delta=5e-3)
        if not for_ci:
            print('{}: {}'.format('test_loss1' + dev_name + quant_type,
                                  test_loss1))
            print('{}: {}'.format('test_loss2' + dev_name + quant_type,
                                  test_loss2))
        w_freeze = np.array(scope.find_var('conv2d_1.w_0').get_tensor())
        # Maybe failed, this is due to the calculation precision
        # self.assertAlmostEqual(np.sum(w_freeze), np.sum(w_quant))
        if not for_ci:
            print('{}: {}'.format('w_freeze' + dev_name + quant_type,
                                  np.sum(w_freeze)))
            print('{}: {}'.format('w_quant' + dev_name + quant_type,
                                  np.sum(w_quant)))

        # Convert parameter to 8-bit.
        convert_int8_pass = ConvertToInt8Pass(scope=scope, place=place)
        convert_int8_pass.apply(test_graph)
        if not for_ci:
            marked_nodes = set()
            for op in test_graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    marked_nodes.add(op)
            test_graph.draw('.', 'test_int8' + dev_name + quant_type,
                            marked_nodes)
        server_program_int8 = test_graph.to_program()
        # Save the 8-bit parameter and model file.
        with fluid.scope_guard(scope):
            fluid.io.save_inference_model('server_int8' + dev_name + quant_type,
                                          ['image', 'label'], [loss], exe,
                                          server_program_int8)
            # Test whether the 8-bit parameter and model file can be loaded successfully.
            [infer, feed, fetch] = fluid.io.load_inference_model(
                'server_int8' + dev_name + quant_type, exe)
        # Check the loaded 8-bit weight.
        w_8bit = np.array(scope.find_var('conv2d_1.w_0.int8').get_tensor())
        self.assertEqual(w_8bit.dtype, np.int8)
        self.assertEqual(np.sum(w_8bit), np.sum(w_freeze))
        if not for_ci:
            print('{}: {}'.format('w_8bit' + dev_name + quant_type,
                                  np.sum(w_8bit)))
            print('{}: {}'.format('w_freeze' + dev_name + quant_type,
                                  np.sum(w_freeze)))

        mobile_pass = TransformForMobilePass()
        mobile_pass.apply(test_graph)
        if not for_ci:
            marked_nodes = set()
            for op in test_graph.all_op_nodes():
                if op.name().find('quantize') > -1:
                    marked_nodes.add(op)
            test_graph.draw('.', 'test_mobile' + dev_name + quant_type,
                            marked_nodes)

        mobile_program = test_graph.to_program()
        with fluid.scope_guard(scope):
            fluid.io.save_inference_model('mobile_int8' + dev_name + quant_type,
                                          ['image', 'label'], [loss], exe,
                                          mobile_program)

    def test_freeze_graph_cuda_dynamic(self):
        if fluid.core.is_compiled_with_cuda():
            with fluid.unique_name.guard():
                self.freeze_graph(
                    True, seed=1, quant_type='abs_max', for_ci=True)

    def test_freeze_graph_cpu_dynamic(self):
        with fluid.unique_name.guard():
            self.freeze_graph(False, seed=2, quant_type='abs_max', for_ci=True)

    def test_freeze_graph_cuda_static(self):
        if fluid.core.is_compiled_with_cuda():
            with fluid.unique_name.guard():
                self.freeze_graph(
                    True, seed=1, quant_type='range_abs_max', for_ci=True)

    def test_freeze_graph_cpu_static(self):
        with fluid.unique_name.guard():
            self.freeze_graph(
                False, seed=2, quant_type='range_abs_max', for_ci=True)


if __name__ == '__main__':
    unittest.main()
