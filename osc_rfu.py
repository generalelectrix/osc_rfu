from __future__ import print_function

import sys
import yaml
import OSC
import logging

from multiprocessing import Process, Queue
from Queue import Empty

"""Start this bitch over.  How do I want this to work?

The RFU should be able to:
- set level on every DMX value in a universe
- step through a named patch list

In general, this program needs to: respond to OSC commands and perform actions,
including sending OSC and acting locally.

Need: a OSC command parser.  Probably set up most of this in main.
"""

UNIV_SIZE = 512

class NumberPad(object):

    keymap = {(0,0): 1,
              (0,1): 4,
              (0,2): 7,
              (0,3): 'C',
              (1,0): 2,
              (1,1): 5,
              (1,2): 8,
              (1,3): 0,
              (2,0): 3,
              (2,1): 6,
              (2,2): 9,
              (2,3): 'E',}

    """Class to handle numeric entry."""
    def __init__(self, buffer_len):
        self.buffer = [0] * buffer_len

    def add_symbol(self, symbol):
        self.buffer.pop(0)
        self.buffer.append(symbol)

    def clear(self):
        self.buffer = [0] * len(self.buffer)

    def parse(self, function):
        return function(''.join(str(v) for v in self.buffer))

    def parse_command(self, x, y):
        key = self.keymap[(x,y)]
        if isinstance(key, int):
            self.add_symbol(key)
        elif key == 'C':
            self.clear()
        elif key == 'E':
            pass
        return key


def unit_float_to_range(start, end, value):
    return int((end-start)*value)+start

def float_to_dmx_val(val):
    return unit_float_to_range(0,255,val)

def dmx_to_float(dmx_val):
    return dmx_val / 255.0

def valid_dmx_addr(addr):
    return addr >= 1 and addr <= 512

class RFU(object):
    """Remote focus unit model."""

    def __init__(self, enttec, osc_handler):
        self.current_chan = 0
        self.enttec = enttec
        self.osc_handler = osc_handler
        self.numpad = NumberPad(3)

    def numpad_action(self, x, y):
        key = self.numpad.parse_command(x, y)
        if key == 'E':
            dmx_addr = self.numpad.parse(int)
            print(dmx_addr)
            if valid_dmx_addr(dmx_addr):
                self.select_channel(dmx_addr)
            
            self.numpad.clear()

        self.osc_handler.set_readout(self.numpad.parse(str))


    def select_channel(self, chan):
        print('selecting channel {}'.format(chan))
        self.current_chan = chan-1
        self.osc_handler.set_current_channel(chan)
        self.osc_handler.set_level(dmx_to_float(self.level))
        self.osc_handler.set_level_indicator(dmx_to_float(self.level))

    @property
    def level(self):
        return self.enttec.dmx_frame[self.current_chan]

    @level.setter
    def level(self, val):
        self.osc_handler.set_level_indicator(dmx_to_float(self.level))
        self.enttec.dmx_frame[self.current_chan] = val
        self.enttec.render()
        if self.debug:
            print('Setting channel {} to {}'.format(self.current_chan+1, self.level))

    def set_level(self, val):
        self.level = val




class OSCController(object):
    """Class to manage oversight of an external OSC control surface."""
    def __init__(self, config):
        self.receiver = OSC.OSCServer( (config['receive_host'], config['receive_port']) )
        self.receiver.addMsgHandler('default', self.handle_osc_message)

        self.sender = OSC.OSCClient()
        self.sender.connect( (config['send_host'], config['send_port']) )
        self.control_groups = {}

    def create_control_group(self, name):
        if name not in self.control_groups:
            self.control_groups[name] = {}

    def create_simple_control(self, group, name, action, preprocessor=None):
        """Create a pure osc listener, with no talkback."""
        if preprocessor is None:
            def callback(_, payload):
                action(payload[0])
        else:
            def callback(_, payload):
                processed = preprocessor(payload[0])
                if processed is not None:
                    action(processed)

        self.control_groups[group][name] = callback

    def create_dmx_entry_pad(self, group, name, number_pad_parser):
        """Number pad control."""
        def callback(addr, payload):
            print("running numpad callback {}".format(addr))
            if ignore_all_but_1(payload[0]) is None:
                return
            elements = addr.split('/')
            group_name = elements[1]
            control_name = elements[2]
            base_addr = '/' + group_name + '/' + control_name + '/{}/{}'
            x = int(elements[3])-1
            y = int(elements[4])-1

            number_pad_parser(x, y)

        self.control_groups[group][name] = callback


    def handle_osc_message(self, addr, type_tags, payload, source_addr):
        elements = addr.split('/')
        if len(elements) < 3:
            return
        group_name = elements[1]
        control_name = elements[2]
        try:
            group = self.control_groups[group_name]
        except KeyError:
            logging.log("Unknown control group: {}".format(group_name))
            return
        try:
            control = group[control_name]
        except KeyError:
            logging.log("Unknown control {} in group {}"
                        .format(control_name, group_name))
        control(addr, payload)

    def send_button_on(self, addr):
        msg = OSC.OSCMessage()
        msg.setAddress(addr)
        msg.append(1.0)
        self.sender.send(msg)

    def send_button_off(self, addr):
        msg = OSC.OSCMessage()
        msg.setAddress(addr)
        msg.append(0.0)
        self.sender.send(msg)

    def send_value(self, addr, val):
        msg = OSC.OSCMessage()
        msg.setAddress(addr)
        msg.append(val)
        self.sender.send(msg)

    def set_readout(self, val):
        self.send_value('/RFU/Readout', val)

    def set_current_channel(self, val):
        self.send_value('/RFU/CurrentChannel', str(val))

    def set_level(self, val):
        self.send_value('/RFU/Level', val)

    def set_level_indicator(self, val):
        self.send_value('/RFU/LevelIndicator', str(float_to_dmx_val(val)))

def ignore_all_but_1(value):
    return value if value == 1.0 else None


if __name__ == '__main__':
    # fire it up!

    import os
    import pyenttec as dmx
    import time
    import threading
    import socket


    try:
        enttec = dmx.select_port()
    except dmx.EnttecPortOpenError as err:
        print(err)
        quit()

    # initialize control streams
    with open('config.yaml') as config_file:
        config = yaml.safe_load(config_file)

    config["receive host"] = socket.gethostbyname(socket.gethostname())
    print("Using local IP address {}".format(config["receive host"]))

    cont = OSCController(config)

    rfu = RFU(enttec, cont)

    cont.create_control_group('RFU')

    cont.create_dmx_entry_pad('RFU', 'DMXEntry', rfu.numpad_action)
    cont.create_simple_control('RFU', 'Level', rfu.set_level, float_to_dmx_val)
    

    rfu.debug = config["debug"]
    rfu.select_channel(1)

    # start the osc server
    # Start OSCServer
    print("\nStarting OSCServer.")
    st = threading.Thread( target = cont.receiver.serve_forever )
    st.start()

    try:
        while True:
            #if debug:
                #try:
                #    print(debug_queue.get(block=False))
                #except Empty:
                #    time.sleep(0.1)
            #else:
            user_input = raw_input('Enter q to quit.')
            if user_input == 'q':
                break


    finally:
        print("\nClosing OSCServer.")
        cont.receiver.close()
        print("Waiting for Server-thread to finish")
        st.join() ##!!!
        print("Done")




