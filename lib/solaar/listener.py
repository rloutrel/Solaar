# -*- python-mode -*-

## Copyright (C) 2012-2013  Daniel Pavel
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License along
## with this program; if not, write to the Free Software Foundation, Inc.,
## 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import errno as _errno
import logging
import time

from collections import namedtuple

import gi

from logitech_receiver import Device, Receiver
from logitech_receiver import base as _base
from logitech_receiver import hidpp10 as _hidpp10
from logitech_receiver import listener as _listener
from logitech_receiver import notifications as _notifications
from logitech_receiver import status as _status

from . import configuration

gi.require_version('Gtk', '3.0')  # NOQA: E402
from gi.repository import GLib  # NOQA: E402 # isort:skip

# from solaar.i18n import _

logger = logging.getLogger(__name__)

_R = _hidpp10.REGISTERS
_IR = _hidpp10.INFO_SUBREGISTERS

#
#
#

_GHOST_DEVICE = namedtuple('_GHOST_DEVICE', ('receiver', 'number', 'name', 'kind', 'status', 'online'))
_GHOST_DEVICE.__bool__ = lambda self: False
_GHOST_DEVICE.__nonzero__ = _GHOST_DEVICE.__bool__
del namedtuple


def _ghost(device):
    return _GHOST_DEVICE(
        receiver=device.receiver, number=device.number, name=device.name, kind=device.kind, status=None, online=False
    )


#
#
#

# how often to poll devices that haven't updated their statuses on their own
# (through notifications)
# _POLL_TICK = 5 * 60  # seconds


class ReceiverListener(_listener.EventsListener):
    """Keeps the status of a Receiver.
    """

    def __init__(self, receiver, status_changed_callback):
        super().__init__(receiver, self._notifications_handler)
        # no reason to enable polling yet
        # self.tick_period = _POLL_TICK
        # self._last_tick = 0

        assert status_changed_callback
        self.status_changed_callback = status_changed_callback
        _status.attach_to(receiver, self._status_changed)

    def has_started(self):
        if logger.isEnabledFor(logging.INFO):
            logger.info('%s: notifications listener has started (%s)', self.receiver, self.receiver.handle)
        nfs = self.receiver.enable_connection_notifications()
        if logger.isEnabledFor(logging.WARNING):
            if not self.receiver.isDevice and not ((nfs if nfs else 0) & _hidpp10.NOTIFICATION_FLAG.wireless):
                logger.warning(
                    'Receiver on %s might not support connection notifications, GUI might not show its devices',
                    self.receiver.path
                )
        self.receiver.status[_status.KEYS.NOTIFICATION_FLAGS] = nfs
        self.receiver.notify_devices()
        self._status_changed(self.receiver)  # , _status.ALERT.NOTIFICATION)

    def has_stopped(self):
        r, self.receiver = self.receiver, None
        assert r is not None
        if logger.isEnabledFor(logging.INFO):
            logger.info('%s: notifications listener has stopped', r)

        # because udev is not notifying us about device removal,
        # make sure to clean up in _all_listeners
        _all_listeners.pop(r.path, None)

        # this causes problems but what is it doing (pfps) - r.status = _('The receiver was unplugged.')
        if r:
            try:
                r.close()
            except Exception:
                logger.exception('closing receiver %s' % r.path)
        self.status_changed_callback(r)  # , _status.ALERT.NOTIFICATION)

    # def tick(self, timestamp):
    #     if not self.tick_period:
    #         raise Exception("tick() should not be called without a tick_period: %s", self)
    #
    #     # not necessary anymore, we're now using udev monitor to watch for receiver status
    #     # if self._last_tick > 0 and timestamp - self._last_tick > _POLL_TICK * 2:
    #     #     # if we missed a couple of polls, most likely the computer went into
    #     #     # sleep, and we have to reinitialize the receiver again
    #     #     logger.warning("%s: possible sleep detected, closing this listener", self.receiver)
    #     #     self.stop()
    #     #     return
    #
    #     self._last_tick = timestamp
    #
    #     try:
    #         # read these in case they haven't been read already
    #         # self.receiver.serial, self.receiver.firmware
    #         if self.receiver.status.lock_open:
    #             # don't mess with stuff while pairing
    #             return
    #
    #         self.receiver.status.poll(timestamp)
    #
    #         # Iterating directly through the reciver would unnecessarily probe
    #         # all possible devices, even unpaired ones.
    #         # Checking for each device number in turn makes sure only already
    #         # known devices are polled.
    #         # This is okay because we should have already known about them all
    #         # long before the first poll() happents, through notifications.
    #         for number in range(1, 6):
    #             if number in self.receiver:
    #                 dev = self.receiver[number]
    #                 if dev and dev.status is not None:
    #                     dev.status.poll(timestamp)
    #     except Exception as e:
    #         logger.exception("polling", e)

    def _status_changed(self, device, alert=_status.ALERT.NONE, reason=None):
        assert device is not None
        if logger.isEnabledFor(logging.INFO):
            try:
                device.ping()
                if device.kind is None:
                    logger.info(
                        'status_changed %r: %s, %s (%X) %s', device, 'present' if bool(device) else 'removed', device.status,
                        alert, reason or ''
                    )
                else:
                    logger.info(
                        'status_changed %r: %s %s, %s (%X) %s', device, 'paired' if bool(device) else 'unpaired',
                        'online' if device.online else 'offline', device.status, alert, reason or ''
                    )
            except Exception:
                logger.info('status_changed for unknown device')

        if device.kind is None:
            assert device == self.receiver
            # the status of the receiver changed
            self.status_changed_callback(device, alert, reason)
            return

        # not true for wired devices - assert device.receiver == self.receiver
        if not device:
            # Device was unpaired, and isn't valid anymore.
            # We replace it with a ghost so that the UI has something to work
            # with while cleaning up.
            if logger.isEnabledFor(logging.INFO):
                logger.info('device %s was unpaired, ghosting', device)
            device = _ghost(device)

        self.status_changed_callback(device, alert, reason)

        if not device:
            # the device was just unpaired, need to update the
            # status of the receiver as well
            self.status_changed_callback(self.receiver)

    def _notifications_handler(self, n):
        assert self.receiver
        # if logger.isEnabledFor(logging.DEBUG):
        #     logger.debug("%s: handling %s", self.receiver, n)
        if n.devnumber == 0xFF:
            # a receiver notification
            _notifications.process(self.receiver, n)
            return

        # a notification that came in to the device listener - strange, but nothing needs to be done here
        if self.receiver.isDevice:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('Notification %s via device %s being ignored.', n, self.receiver)
            return

        # DJ pairing notification - ignore - hid++ 1.0 pairing notification is all that is needed
        if n.sub_id == 0x41 and n.report_id == _base.DJ_MESSAGE_ID:
            if logger.isEnabledFor(logging.INFO):
                logger.info('ignoring DJ pairing notification %s', n)
            return

        # a device notification
        if not (0 < n.devnumber <= 16):  # some receivers have devices past their max # devices
            if logger.isEnabledFor(logging.WARNING):
                logger.warning('Unexpected device number (%s) in notification %s.', n.devnumber, n)
            return
        already_known = n.devnumber in self.receiver

        # FIXME: hacky fix for kernel/hardware race condition
        # If the device was just turned on or woken up from sleep, it may not
        # be ready to receive commands. The "payload" bit of the wireless
        # status notification seems to tell us this. If this is the case, we
        # must wait a short amount of time to avoid causing a broken pipe
        # error.
        device_ready = not bool(ord(n.data[0:1]) & 0x80) or n.sub_id != 0x41
        if not device_ready:
            time.sleep(0.01)

        if n.sub_id == 0x40 and not already_known:
            return  # disconnecting something that is not known - nothing to do

        if n.sub_id == 0x41:
            if not already_known:
                if n.address == 0x0A and not self.receiver.receiver_kind == 'bolt':
                    # some Nanos send a notification even if no new pairing - check that there really is a device there
                    if self.receiver.read_register(_R.receiver_info, _IR.pairing_information + n.devnumber - 1) is None:
                        return
                dev = self.receiver.register_new_device(n.devnumber, n)
            elif self.receiver.status.lock_open and self.receiver.re_pairs and not ord(n.data[0:1]) & 0x40:
                dev = self.receiver[n.devnumber]
                del self.receiver[n.devnumber]  # get rid of information on device re-paired away
                self._status_changed(dev)  # signal that this device has changed
                dev = self.receiver.register_new_device(n.devnumber, n)
                self.receiver.status.new_device = self.receiver[n.devnumber]
            else:
                dev = self.receiver[n.devnumber]
        else:
            dev = self.receiver[n.devnumber]

        if not dev:
            logger.warning('%s: received %s for invalid device %d: %r', self.receiver, n, n.devnumber, dev)
            return

        # Apply settings every time the device connects
        if n.sub_id == 0x41:
            if logger.isEnabledFor(logging.INFO):
                try:
                    dev.ping()
                    logger.info('connection %s for %r', n, dev)
                except Exception:
                    logger.info('connection %s for unknown device, number %s', n, n.devnumber)
            # If there are saved configs, bring the device's settings up-to-date.
            # They will be applied when the device is marked as online.
            configuration.attach_to(dev)
            _status.attach_to(dev, self._status_changed)
            # the receiver changed status as well
            self._status_changed(self.receiver)

        if not hasattr(dev, 'status') or dev.status is None:
            # notification before device status set up - don't process it
            logger.warning('%s before device %s has status', n, dev)
        else:
            _notifications.process(dev, n)

        if self.receiver.status.lock_open and not already_known:
            # this should be the first notification after a device was paired
            assert n.sub_id == 0x41, 'first notification was not a connection notification'
            if logger.isEnabledFor(logging.INFO):
                logger.info('%s: pairing detected new device', self.receiver)
            self.receiver.status.new_device = dev
        elif dev.online is None:
            dev.ping()

    def __str__(self):
        return '<ReceiverListener(%s,%s)>' % (self.receiver.path, self.receiver.handle)


#
#
#

# all known receiver listeners
# listeners that stop on their own may remain here
_all_listeners = {}


def _start(device_info):
    assert _status_callback
    isDevice = device_info.isDevice
    if not isDevice:
        receiver = Receiver.open(device_info)
    else:
        receiver = Device.open(device_info)
        configuration.attach_to(receiver)

    if receiver:
        rl = ReceiverListener(receiver, _status_callback)
        rl.start()
        _all_listeners[device_info.path] = rl
        return rl

    logger.warning('failed to open %s', device_info)


def start_all():
    # just in case this it called twice in a row...
    stop_all()

    if logger.isEnabledFor(logging.INFO):
        logger.info('starting receiver listening threads')
    for device_info in _base.receivers_and_devices():
        _process_receiver_event('add', device_info)


def stop_all():
    listeners = list(_all_listeners.values())
    _all_listeners.clear()

    if listeners:
        if logger.isEnabledFor(logging.INFO):
            logger.info('stopping receiver listening threads %s', listeners)

        for l in listeners:
            l.stop()

    configuration.save()

    if listeners:
        for l in listeners:
            l.join()


# ping all devices to find out whether they are connected
# after a resume, the device may have been off
# so mark its saved status to ensure that the status is pushed to the device when it comes back
def ping_all(resuming=False):
    if logger.isEnabledFor(logging.INFO):
        logger.info('ping all devices%s', ' when resuming' if resuming else '')
    for l in _all_listeners.values():
        if l.receiver.isDevice:
            if resuming and hasattr(l.receiver, 'status'):
                l.receiver.status._active = None  # ensure that settings are pushed
            if l.receiver.ping():
                l.receiver.status.changed(active=True, push=True)
            l._status_changed(l.receiver)
        else:
            count = l.receiver.count()
            if count:
                for dev in l.receiver:
                    if resuming and hasattr(dev, 'status'):
                        dev.status._active = None  # ensure that settings are pushed
                    if dev.ping():
                        dev.status.changed(active=True, push=True)
                    l._status_changed(dev)
                    count -= 1
                    if not count:
                        break


_status_callback = None
_error_callback = None


def setup_scanner(status_changed_callback, error_callback):
    global _status_callback, _error_callback
    assert _status_callback is None, 'scanner was already set-up'

    _status_callback = status_changed_callback
    _error_callback = error_callback

    _base.notify_on_receivers_glib(_process_receiver_event)


def _process_add(device_info, retry):
    try:
        _start(device_info)
    except OSError as e:
        if e.errno == _errno.EACCES:
            try:
                import subprocess
                output = subprocess.check_output(['/usr/bin/getfacl', '-p', device_info.path], text=True)
                if logger.isEnabledFor(logging.WARNING):
                    logger.warning('Missing permissions on %s\n%s.', device_info.path, output)
            except Exception:
                pass
            if retry:
                GLib.timeout_add(2000.0, _process_add, device_info, retry - 1)
            else:
                _error_callback('permissions', device_info.path)
        else:
            _error_callback('nodevice', device_info.path)
    except _base.NoReceiver:
        _error_callback('nodevice', device_info.path)


# receiver add/remove events will start/stop listener threads
def _process_receiver_event(action, device_info):
    assert action is not None
    assert device_info is not None
    assert _error_callback

    if logger.isEnabledFor(logging.INFO):
        logger.info('receiver event %s %s', action, device_info)

    # whatever the action, stop any previous receivers at this path
    l = _all_listeners.pop(device_info.path, None)
    if l is not None:
        assert isinstance(l, ReceiverListener)
        l.stop()

    if action == 'add':  # a new device was detected
        _process_add(device_info, 3)

    return False
