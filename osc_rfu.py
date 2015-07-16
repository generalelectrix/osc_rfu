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

class RFUBackend(object):
    """The common backend for all RFU instances."""
    def __init__(self, enttec, osc_handler, debug):
        self.enttec = enttec
        self.rfus = {}
        self.osc_handler = osc_handler
        self.debug = debug

    def add_rfu(self, ipaddr):
        rfu = RFU(self.osc_handler, ipaddr, self)
        self.rfus[ipaddr] = rfu
        self.osc_handler.add_sender(ipaddr)
        rfu.select_channel(1)

    def remove_rfu(self, ipaddr):
        try:
            del self.rfus[ipaddr]
            self.osc_handler.remove_sender(ipaddr)
        except KeyError:
            pass

    def list_rfus(self):
        return self.rfus.keys()

    def numpad_action(self, ipaddr, x, y):
        try:
            self.rfus[ipaddr].numpad_action(x, y)
        except KeyError:
            print("Numpad action from unknown RFU: {}".format(ipaddr))

    def set_level_action(self, ipaddr, level):
        try:
            self.rfus[ipaddr].set_level(level)
        except KeyError:
            print("Set level action from unknown RFU: {}".format(ipaddr))

    def get_level(self, chan):
        return self.enttec.dmx_frame[chan-1]

    def set_level(self, chan, val):
        self.enttec.dmx_frame[chan-1] = val
        self.enttec.render()
        self.update_level(chan)

    def update_level(self, chan):
        level = self.get_level(chan)
        for rfu in self.rfus.itervalues():
            if rfu.current_chan == chan:
                rfu.level = level

class RFU(object):
    """Remote focus unit model."""

    def __init__(self, osc_handler, ipaddr, backend):
        self.ipaddr = ipaddr
        self.current_chan = 0
        self.osc_handler = osc_handler
        self.numpad = NumberPad(3)
        self.backend = backend
        self.debug = backend.debug

    def numpad_action(self, x, y):
        key = self.numpad.parse_command(x, y)
        if key == 'E':
            dmx_addr = self.numpad.parse(int)
            print(dmx_addr)
            if valid_dmx_addr(dmx_addr):
                self.select_channel(dmx_addr)
            
            self.numpad.clear()

        self.osc_handler.set_readout(self.ipaddr, self.numpad.parse(str))


    def select_channel(self, chan):
        print('selecting channel {}'.format(chan))
        self.current_chan = chan

        self.osc_handler.set_current_channel(self.ipaddr, chan)
        self.osc_handler.set_level(self.ipaddr, dmx_to_float(self.level))
        self.osc_handler.set_level_indicator(self.ipaddr, dmx_to_float(self.level))

    @property
    def level(self):
        return self.backend.get_level(self.current_chan)

    @level.setter
    def level(self, val):
        self.osc_handler.set_level_indicator(self.ipaddr, dmx_to_float(self.level))
        if self.debug:
            print('Setting channel {} to {}'.format(self.current_chan, self.level))

    def set_level(self, val):
        self.backend.set_level(self.current_chan, val)




class OSCController(object):
    """Class to manage oversight of an external OSC control surface."""
    def __init__(self, config):
        self.receiver = OSC.OSCServer( (config['receive_host'], config['receive_port']) )
        self.receiver.addMsgHandler('default', self.handle_osc_message)

        self.senders = {}

        self.control_groups = {}

    def add_sender(self, ipaddr, port=9000):
        sender = OSC.OSCClient()
        sender.connect( (ipaddr, port) )
        self.senders[ipaddr] = sender

    def remove_sender(self, ipaddr):
        try:
            del self.senders[ipaddr]
        except KeyError:
            pass

    def create_control_group(self, name):
        if name not in self.control_groups:
            self.control_groups[name] = {}

    def create_simple_control(self, group, name, action, preprocessor=None):
        """Create a pure osc listener, with no talkback."""
        if preprocessor is None:
            def callback(ipaddr, _, payload):
                action(ipaddr, payload[0])
        else:
            def callback(ipaddr, _, payload):
                processed = preprocessor(payload[0])
                if processed is not None:
                    action(ipaddr, processed)

        self.control_groups[group][name] = callback

    def create_dmx_entry_pad(self, group, name, number_pad_parser):
        """Number pad control."""
        def callback(ipaddr, addr, payload):
            print("running numpad callback {}".format(addr))
            if ignore_all_but_1(payload[0]) is None:
                return
            elements = addr.split('/')
            group_name = elements[1]
            control_name = elements[2]
            base_addr = '/' + group_name + '/' + control_name + '/{}/{}'
            x = int(elements[3])-1
            y = int(elements[4])-1

            number_pad_parser(ipaddr, x, y)

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
            print("Unknown control group: {}".format(group_name))
            return
        try:
            control = group[control_name]
        except KeyError:
            print("Unknown control {} in group {}"
                  .format(control_name, group_name))
        control(source_addr[0], addr, payload)

    def send_button_on(self, ipaddr, addr):
        msg = OSC.OSCMessage()
        msg.setAddress(addr)
        msg.append(1.0)
        self.senders[ipaddr].send(msg)

    def send_button_off(self, ipaddr, addr):
        msg = OSC.OSCMessage()
        msg.setAddress(addr)
        msg.append(0.0)
        self.senders[ipaddr].send(msg)

    def send_value(self, ipaddr, addr, val):
        msg = OSC.OSCMessage()
        msg.setAddress(addr)
        msg.append(val)
        self.senders[ipaddr].send(msg)

    def set_readout(self, ipaddr, val):
        self.send_value(ipaddr, '/RFU/Readout', val)

    def set_current_channel(self, ipaddr, val):
        self.send_value(ipaddr, '/RFU/CurrentChannel', str(val))

    def set_level(self, ipaddr, val):
        self.send_value(ipaddr, '/RFU/Level', val)

    def set_level_indicator(self, ipaddr, val):
        self.send_value(ipaddr, '/RFU/LevelIndicator', str(float_to_dmx_val(val)))

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

    rfus = RFUBackend(enttec, cont, config["debug"])

    cont.create_control_group('RFU')

    cont.create_dmx_entry_pad('RFU', 'DMXEntry', rfus.numpad_action)
    cont.create_simple_control('RFU', 'Level', rfus.set_level_action, float_to_dmx_val)
    

    # start the osc server
    # Start OSCServer
    print("\nStarting OSCServer.")
    st = threading.Thread( target = cont.receiver.serve_forever )
    st.start()

    for ipaddr in config["send_hosts"]:
        print(ipaddr)
        rfus.add_rfu(ipaddr)

    try:
        while True:
            #if debug:
                #try:
                #    print(debug_queue.get(block=False))
                #except Empty:
                #    time.sleep(0.1)
            #else:
            print("Commands:\n"
                  "add:12.34.56.78 to add a new RFU.\n"
                  "del:12.34.56.78 to remove an RFU.\n"
                  "list to list connected RFUs\n"
                  "help to diplay this message\n"
                  "q to quit")
            user_input = raw_input('Enter command:')
            if user_input == 'q':
                break
            elif user_input == 'help':
                continue
            elif user_input == 'list':
                print(rfus.list_rfus())
            elif user_input.startswith('add:'):
                ipaddr = user_input.split(':')[1]
                rfus.add_rfu(ipaddr)

            elif user_input.startswith('del:'):
                ipaddr = user_input.split(':')[1]
                rfus.remove_rfu(ipaddr)


    finally:
        print("\nClosing OSCServer.")
        cont.receiver.close()
        print("Waiting for Server-thread to finish")
        st.join() ##!!!
        print("Done")




