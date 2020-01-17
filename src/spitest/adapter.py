"""Demo adapter for ODIN control workshop

This class implements a simple adapter used for demonstration purposes in a

Tim Nicholls, STFC Application Engineering
"""
import logging
import tornado
import time
import sys
import random
from concurrent import futures

from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.concurrent import run_on_executor
from tornado.escape import json_decode

from odin.adapters.adapter import ApiAdapter, ApiAdapterResponse, request_types, response_types
from odin.adapters.parameter_tree import ParameterTree, ParameterTreeError
from odin._version import get_versions

from odin_devices.mcp23008 import MCP23008

# Add the thermocouple to this adapter to get it working more easily
# Will make my own adapter from scratch another time
from odin_devices.max31856 import Max31856


class WorkshopAdapter(ApiAdapter):
    """System info adapter class for the ODIN server.

    This adapter provides ODIN clients with information about the server and the system that it is
    running on.
    """

    def __init__(self, **kwargs):
        """Initialize the WorkshopAdapter object.

        This constructor initializes the WorkshopAdapter object.

        :param kwargs: keyword arguments specifying options
        """
        # Intialise superclass
        super(WorkshopAdapter, self).__init__(**kwargs)

        # Parse options
        self.LED_task_enable = bool(self.options.get('LED_task_enable', True))
        self.LED_task_interval = float(self.options.get('LED_task_interval', 0.25))
        self.temp_task_enable = bool(self.options.get('temp_task_enable', True))

        self.workshop = Workshop(self.LED_task_enable, self.LED_task_interval, self.temp_task_enable)

        logging.debug('WorkshopAdapter loaded')

    @response_types('application/json', default='application/json')
    def get(self, path, request):
        """Handle an HTTP GET request.

        This method handles an HTTP GET request, returning a JSON response.

        :param path: URI path of request
        :param request: HTTP request object
        :return: an ApiAdapterResponse object containing the appropriate response
        """
        try:
            response = self.workshop.get(path)
            status_code = 200
        except ParameterTreeError as e:
            response = {'error': str(e)}
            status_code = 400

        content_type = 'application/json'

        return ApiAdapterResponse(response, content_type=content_type,
                                  status_code=status_code)

    @request_types('application/json')
    @response_types('application/json', default='application/json')
    def put(self, path, request):
        """Handle an HTTP PUT request.

        This method handles an HTTP PUT request, returning a JSON response.

        :param path: URI path of request
        :param request: HTTP request object
        :return: an ApiAdapterResponse object containing the appropriate response
        """

        content_type = 'application/json'

        try:
            data = json_decode(request.body)
            self.workshop.set(path, data)
            response = self.workshop.get(path)
            status_code = 200
        except WorkshopError as e:
            response = {'error': str(e)}
            status_code = 400
        except (TypeError, ValueError) as e:
            response = {'error': 'Failed to decode PUT request body: {}'.format(str(e))}
            status_code = 400

        logging.debug(response)

        return ApiAdapterResponse(response, content_type=content_type,
                                  status_code=status_code)

    def delete(self, path, request):
        """Handle an HTTP DELETE request.

        This method handles an HTTP DELETE request, returning a JSON response.

        :param path: URI path of request
        :param request: HTTP request object
        :return: an ApiAdapterResponse object containing the appropriate response
        """
        response = 'WorkshopAdapter: DELETE on path {}'.format(path)
        status_code = 200

        logging.debug(response)

        return ApiAdapterResponse(response, status_code=status_code)

    def cleanup(self):
        """Clean up adapter state at shutdown.

        This method cleans up the adapter state when called by the server at e.g. shutdown.
        It simplied calls the cleanup function of the workshop instance.
        """
        self.workshop.cleanup()

class WorkshopError(Exception):
    """Simple exception class to wrap lower-level exceptions."""

    pass


class Workshop():
    """Workshop - class that extracts and stores information about system-level parameters."""

    # Thread executor used for background tasks
    executor = futures.ThreadPoolExecutor(max_workers=1)

    # Setting up pins
    RED = 2
    YELLOW = 1
    GREEN = 0

    def __init__(self, LED_task_enable, LED_task_interval, temp_task_enable):
        """Initialise the Workshop object.

        This constructor initlialises the Workshop object, building a parameter tree and
        launching a background task if enabled
        """

        # Store initialisation time
        self.init_time = time.time()

        # Get package version information
        version_info = get_versions()

        # Initialise MCP23008 device
        self.mcp = MCP23008(address=0x20, busnum=2)
        num_pins = 3
        for pin in range(num_pins):
           self.mcp.setup(pin, MCP23008.OUT)
           self.mcp.output(pin, 0)
        self.led_states = [0] * num_pins

        # Set up thermocouple instance and variables
        self.thermoC = Max31856()
        self.avg_temp = 0
        self.avg_temp_calc = [0] * 10
        self.avg_count = 0
        self.ten_count_switch = False
        self.temp_task_enable = temp_task_enable
        self.temp_bounds = [21.50, 22.00]

        # Save LED_task arguments
        self.task_mode = 'command'
        self.LED_task_enable = LED_task_enable
        self.LED_task_interval = LED_task_interval

        # Set the background task counters to zero
        self.rave_ioloop_counter = 0
        self.traffic_wait_counter = 0
        self.traffic_loop_counter = 0
        self.temp_count = 0
        self.background_thread_counter = 0 # not using the thread

        # Tell user default mode for LEDs
        logging.debug('LED mode set to default: {}.'.format(self.task_mode))

        # Build a parameter tree for the background task
        LED_task = ParameterTree({
            'rave_count': (lambda: self.rave_ioloop_counter, None),
            'traffic_count': (lambda: self.traffic_loop_counter, None),
            'enable': (lambda: self.LED_task_enable, self.LED_task_enable),
            'task_mode': (
                      lambda: self.task_mode,
                      lambda mode: self.set_task_mode(mode)
                      ),
            'interval': (lambda: self.LED_task_interval, self.set_LED_task_interval),
        })

        # A parameter tree for the LEDs to interact with
        led_tree = ParameterTree({
            'red':    (
		lambda: self.led_states[self.RED],
		lambda state: self.set_led_state(self.RED, state)
            ),
            'yellow': (
                lambda: self.led_states[self.YELLOW],
                lambda state: self.set_led_state(self.YELLOW, state)
            ),
            'green':  (
                lambda: self.led_states[self.GREEN],
                lambda state: self.set_led_state(self.GREEN, state)
            ),
        })

        # Sub-tree to change temperature boundaries
        thermo_bound_tree = ParameterTree({
            'lower': (
                lambda: self.temp_bounds[0],
                lambda temp: self.set_temp_bounds(0, temp)
            ),
            'upper': (
                lambda: self.temp_bounds[1],
                lambda temp: self.set_temp_bounds(1, temp)
            )
        })

        # Parameter tree for the thermocouple
        thermo_tree = ParameterTree({
        'temperature': (lambda: self.thermoC.temperature, None),
        'rolling_avg': (lambda:self.avg_temp, None),
        'temps_counted': (lambda: self.temp_count, None),
        'temp_bounds': thermo_bound_tree,
        })

        # Store all information in a parameter tree
        self.param_tree = ParameterTree({
            'odin_version': version_info['version'],
            'tornado_version': tornado.version,
            'server_uptime': (self.get_server_uptime, None),
            'LED_task': LED_task,
            'leds': led_tree,
            'temperature': thermo_tree,
        })

        # Launch the background tasks if enabled in options
        if self.LED_task_enable:
            self.start_LED_task()

        if self.temp_task_enable:
            self.start_temp_task()

    def set_task_mode(self, mode):
        logging.debug('setting task mode to {}'.format(mode))
        self.task_mode = str(mode)

    def set_led_state(self, led, state):
        self.led_states[led] = int(state)
        logging.info('Setting LED {} state to {}'.format(led, state))
        self.mcp.output(led, state)

    def set_temp_bounds(self, bound, temp):
        self.temp_bounds[bound] = temp
        if bound == 0:
            bound = 'Lower'
        elif bound == 1:
            bound = 'Upper'
        else:
            print('Invalid bound provided. Should not be seen.')
            pass

        logging.info('{} bound set to {}'.format(bound, temp))

    def get_server_uptime(self):
        """Get the uptime for the ODIN server.

        This method returns the current uptime for the ODIN server.
        """
        return time.time() - self.init_time

    def get(self, path):
        """Get the parameter tree.

        This method returns the parameter tree for use by clients via the Workshop adapter.

        :param path: path to retrieve from tree
        """
        return self.param_tree.get(path)

    def set(self, path, data):
        """Set parameters in the parameter tree.

        This method simply wraps underlying ParameterTree method so that an exceptions can be
        re-raised with an appropriate WorkshopError.

        :param path: path of parameter tree to set values for
        :param data: dictionary of new data values to set in the parameter tree
        """
        try:
            self.param_tree.set(path, data)
        except ParameterTreeError as e:
            raise WorkshopError(e)

    def cleanup(self):
        """Clean up the Workshop instance.

        This method stops the background tasks, allowing the adapter state to be cleaned up
        correctly.
        """
        self.stop_LED_task()

########
    def set_LED_task_interval(self, interval):
        """Set the background task interval."""
        logging.debug("Setting background task interval to %f", interval)
        self.LED_task_interval = float(interval)
    def set_LED_task_enable(self, enable):
        """Set the background task enable."""
        enable = bool(enable)

        if enable != self.LED_task_enable:
            if enable:
                self.start_LED_task()
            else:
                self.stop_LED_task()
########

    def start_LED_task(self):
        """Start the background tasks."""
        logging.debug(
            "Launching background tasks with interval %.2f secs", self.LED_task_interval
        )
        self.LED_task_enable = True

        # Register a periodic callback for the ioloop task and start it
        self.LED_ioloop_task = PeriodicCallback(
            self.LED_ioloop_callback, self.LED_task_interval * 1000
        )
        self.LED_ioloop_task.start()

        # Run the background thread task in the thread execution pool
#        self.background_thread_task()

    def stop_LED_task(self):
        """Stop the background tasks."""
        self.LED_task_enable = False
        self.LED_ioloop_task.stop()

    def update_led(self, led, state):
        '''A function to turn an LED on, and to update its state in led_states,
           to save on code duplication and in case another theoretical device
           wants to be used.'''
        self.mcp.output(led, state)
        self.led_states[led] = int(state)

    def LED_ioloop_callback(self):
        '''Run the LED ioloop callback. It should randomly switch LEDS off and on
           whenever it is called, it won't always just switch them.'''

        # RAVE task
        if self.task_mode == 'rave':
            for i in range(3):
                self.update_led(random.randint(0, 2), random.randint(0, 1))
            self.rave_ioloop_counter += 1

        # Traffic task
        if self.task_mode == 'traffic':
            self.traffic_wait_counter += 1

            if self.traffic_wait_counter == 2: # 1 added when called
                self.update_led(self.YELLOW, 0)  # assuming interval=0.25s
                self.update_led(self.RED, 1)

            elif self.traffic_wait_counter == 14: # 2+12 (+3s default)
                self.update_led(self.YELLOW, 1)

            elif self.traffic_wait_counter == 22: # 14+8 (+2s)
                self.update_led(self.RED, 0)
                self.update_led(self.YELLOW, 0)
                self.update_led(self.GREEN, 1)

            elif self.traffic_wait_counter == 34: # 22+12 (+3s)
                self.update_led(self.GREEN, 0)
                self.update_led(self.YELLOW, 1)

            elif self.traffic_wait_counter == 39: # 36+3, +1 added waiting to start over
                self.traffic_wait_counter = 0  # 2s on this one
                self.traffic_loop_counter += 1
        # Thermometer and command mode
        else:  # Both are handled here. Command has no task, and
             pass  # it makes more sense to put thermometer with temp_task

    def start_temp_task(self):
        """Start the thermocouple task."""
        self.temp_task_enable = True

        self.temp_ioloop_task = PeriodicCallback(
            self.temp_ioloop_callback, 1000
        )  # Interval set to 1 second, no reason to add a variable interval.

        self.temp_ioloop_task.start()

    def stop_temp_task(self):
        """Stop the thermocouple task."""
        self.temp_task_enable = False
        self.temp_ioloop_task.stop()

    def temp_ioloop_callback(self):
        """Thermocouple callback task.
           Once per second, read the temperature.
           If in the correct mode, interact with the LEDs from here as well.
        """
        print("Thermocouple temperature is {:.1f} C".format(self.thermoC.temperature))
        temperature = self.thermoC.temperature

        #Calculating the rolling average
        self.avg_temp = 0
        self.avg_temp_calc[self.avg_count] = temperature
        self.avg_count += 1
        for temp in self.avg_temp_calc:
            self.avg_temp += temp

        if self.ten_count_switch:
            self.avg_temp /= 10
            if self.avg_count == 10: # count still needs reset at 10
                self.avg_count = 0
        else:
            if self.avg_count < 10:
                self.avg_temp /= self.avg_count
            else:
                self.ten_count_switch = True # once 10 temps recorded
                self.avg_temp /= 10          # always divide by 10 for avg
                self.avg_count = 0

        self.temp_count += 1

        if self.task_mode == 'thermometer':

            self.update_led(self.RED, 0)  # Turn LEDs off so that
            self.update_led(self.YELLOW, 0)  # only one is on at once
            self.update_led(self.GREEN, 0)

            if temperature < self.temp_bounds[0]: # < Lower bound
                self.update_led(self.YELLOW, 1)
            elif temperature < self.temp_bounds[1] and temperature > self.temp_bounds[0]:
            # elif lower < temp < upper
                 self.update_led(self.GREEN, 1)
            elif temperature > self.temp_bounds[1]:
                 self.update_led(self.RED, 1)




    @run_on_executor
    def background_thread_task(self):
        """The the adapter background thread task.

        This method runs in the thread executor pool, sleeping for the specified interval and 
        incrementing its counter once per loop, until the background task enable is set to false.
        """

        sleep_interval = self.background_task_interval

        while self.background_task_enable:
            time.sleep(sleep_interval)
            if self.background_thread_counter < 10 or self.background_thread_counter % 20 == 0:
                logging.debug(
                    "Background thread task running, count = %d", self.background_thread_counter
                )
            self.background_thread_counter += 1

        logging.debug("Background thread task stopping")


