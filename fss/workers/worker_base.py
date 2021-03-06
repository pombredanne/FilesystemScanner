import logging
import time
import queue

import fss.config.workers

_LOGGER = logging.getLogger(__name__)


class WorkerBase(object):
    def __init__(self, pipeline_state, input_q, output_q, log_q, quit_ev):
        self.__pipeline_state = pipeline_state
        self.__input_q = input_q
        self.__output_q = output_q
        self.__log_q = log_q
        self.__quit_ev = quit_ev
        
        self.__tick = 0
        self.__push_count = 0
        self.__read_count = 0
        self.__last_check_epoch = None

    def log(self, log_type, message, *args):
        message = message % args
        self.__log_q.put((self.__class__.__name__, log_type, message))

    def wait_for_log_empty(self):
        while True:
            if self.__log_q.empty() is True:
                _LOGGER.debug("Log queue is empty: [%s]", 
                              self.__class__.__name__)
                break

            time.sleep(fss.config.workers.SHUTDOWN_LOG_DEPLETE_CHECK_INTERVAL_S)

    def increment_tick(self):
        """This should be called during every cycle of any loop, so that we can 
        interleave other tasks.
        """

        self.__tick += 1

    def check_quit(self):
# TODO(dustin): This seems to be running too frequently.
        if self.__tick % fss.config.workers.QUIT_CHECK_TICK_INTERVAL == 0 or \
           self.__last_check_epoch is None or \
           (time.time() - self.__last_check_epoch) > fss.config.workers.QUIT_CHECK_INTERVAL_S:

            if self.quit_ev.is_set() is True:
                self.log(
                    logging.INFO, 
                    "[%s] component terminated.", 
                    component_name)

                return True

            self.__last_check_epoch = time.time()

        return False

    def __set_data(self, key, value):
        component_name = self.get_component_name()
        self.pipeline_state['data_' + component_name + '_' + key] = value

    def __set_state(self, state):
        component_name = self.get_component_name()
        self.pipeline_state['running_' + component_name] = state

    def __get_state(self, component_name):
        return self.pipeline_state['running_' + component_name]

    def __get_data(self, component_name, key):
        return self.pipeline_state['data_' + component_name + '_' + key]

    def push_to_output(self, item):
        self.__push_count += 1
        self.output_q.put(item)

    def set_finished(self):
        """This stores the number of items that have been pushed, and 
        transitions the current component to the FINISHED state (which precedes 
        the STOPPED state). The FINISHED state isn't really necessary unless 
        methods/hooks are overridden to depend on it, but the count must be 
        stored at one point so that thenext components knows how many items to 
        expect. This is done by default after the loop breaks, but can be 
        manually called sooner, if desired.
        """

        component_name = self.get_component_name()

        self.log(
            logging.INFO,
            "Component [%s] is being marked as finished.", 
            component_name)

        existing_state = self.__get_state(component_name)

        assert existing_state == fss.constants.PCS_RUNNING, \
               "Can not change to 'finished' state from unsupported " \
               "state: (" + str(existing_state) + ")"

        assert self.__push_count > 0, \
               "Finish-count must be greater than zero."

        self.__set_data('count', self.__push_count)
        self.__set_state(fss.constants.PCS_FINISHED)

    def __handle_queue_idle(self):
        component_name = self.get_component_name()

        # If we're starved and the upstream component has stopped, 
        # terminate.
        try:
            upstream_component_name = self.get_upstream_component_name()
        except NotImplementedError:
            # No upstream component is defined.

            self.log(
                logging.INFO, 
                "Component [%s] is idle and there's no upstream "
                "component. Stopping.",
                component_name)

            return False

        # An upstream component is defined.

        upstream_state = self.__get_state(upstream_component_name)
        if upstream_state == fss.constants.PCS_STOPPED:
            need_count = self.__get_data(
                            upstream_component_name, 
                            'count')

            if self.__read_count == need_count:

                # Automatically mark this component has finished when 
                # the previous component has stopped, and we've 
                # processed as many items as we're queued.
#                self.set_finished()

                self.log(
                    logging.INFO, 
                    "Component [%s] is idle, upstream component [%s] has "
                    "ended, and we have consumed all items (%d). Stopping.",
                    component_name, 
                    upstream_component_name,
                    self.__read_count)

                return False
            elif self.__read_count > need_count:
                self.log(
                    logging.ERROR,
                    "Component [%s] has received more items than " \
                    "were pushed by upstream [%s]: (%d) > (%d)",
                    component_name,
                    upstream_component_name,
                    self.__read_count,
                    need_count)
            else:
                self.log(
                    logging.DEBUG, 
                    "Upstream component [%s] has ended, but " \
                    "downstream component [%s] is not caught up, " \
                    "yet: have (%d) != need (%d)",
                    upstream_component_name,
                    component_name,
                    self.__read_count,
                    need_count)

        if self.check_quit() is True:
            return False

        if self.loop_idle_hook() is False:
            self.log(
                logging.INFO,
                "Component [%s] is idle and we've been told to break.",
                component_name)

            return False

        time.sleep(fss.config.workers.WORKER_IDLE_SLEEP_S)

        self.__tick += 1

    def run(self):
        component_name = self.get_component_name()

        self.__set_state(fss.constants.PCS_RUNNING)

        self.log(logging.INFO, "[%s] component running.", component_name)

        self.pre_loop_hook()
        
        while True:
            try:
                item = self.input_q.get(block=False)
            except queue.Empty:
                if self.__handle_queue_idle() is False:
                    break

                continue

            self.__read_count += 1
#            self.log(logging.INFO, "Component [%s] new read-count: (%d) -- %s", component_name, self.__read_count, item)

            if self.check_quit() is True:
                break

            if self.__tick % fss.config.workers.PROGRESS_LOG_TICK_INTERVAL == 0:
                self.log(
                    logging.DEBUG, 
                    "Component [%s] progress: (%d)", 
                    component_name, self.__tick)

            if self.process_item(item) is False:
                self.log(
                    logging.INFO, 
                    "Item process for component [%s] has requested loop "
                    "termination.", 
                    component_name)

                break

            self.__tick += 1

        self.post_loop_hook()

        self.log(
            logging.INFO, 
            "Component [%s] loop has terminated.", 
            component_name)

        self.wait_for_log_empty()

        self.__set_state(fss.constants.PCS_STOPPED)

    @property
    def pipeline_state(self):
        return self.__pipeline_state

    @property
    def input_q(self):
        return self.__input_q

    @property
    def output_q(self):
        return self.__output_q

    @property
    def quit_ev(self):
        return self.__quit_ev

    @property
    def tick_count(self):
# TODO(dustin): Rename the member-variable to "__tick_count".
        return self.__tick

    def loop_idle_hook(self):
        pass

    def pre_loop_hook(self):
        pass

    def post_loop_hook(self):
        self.set_finished()

    def process_item(self, item):
        raise NotImplementedError()

    def get_component_name(self):
        raise NotImplementedError()

    def get_upstream_component_name(self):
        raise NotImplementedError()
