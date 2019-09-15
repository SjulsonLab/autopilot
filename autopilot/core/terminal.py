__version__ = '0.2'
__author__  = 'Jonny Saunders <JLSaunders987@gmail.com>'

import argparse
import json
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rpilot import prefs
from rpilot.core import styles

if __name__ == '__main__':
    # Parse arguments - this should have been called with a .json prefs file passed
    # We'll try to look in the default location first
    parser = argparse.ArgumentParser(description="Run an RPilot Terminal")
    parser.add_argument('-f', '--prefs', help="Location of .json prefs file (created during setup_terminal.py)")
    args = parser.parse_args()

    if not args.prefs:
        prefs_file = '/usr/rpilot/prefs.json'

        if not os.path.exists(prefs_file):
            raise Exception("No Prefs file passed, and file not in default location")

        raise Warning('No prefs file passed, loaded from default location. Should pass explicitly with -p')

    else:
        prefs_file = args.prefs

    # init prefs for module access
    prefs.init(prefs_file)


import datetime
import logging
import threading
from collections import OrderedDict as odict
import numpy as np

from PySide import QtCore, QtGui, QtSvg
from subject import Subject
from plots import Plot_Widget
from networking import Terminal_Station, Net_Node
from utils import InvokeEvent, Invoker
from gui import Control_Panel, Protocol_Wizard, Weights, Reassign, Calibrate_Water, Bandwidth_Test
import pdb


# TODO: Be more complete about generating logs
# TODO: Make exit graceful
# TODO: Make 'edit subject' button
# TODO: Make experiment tags, save and populate?

# http://zetcode.com/gui/pysidetutorial/layoutmanagement/
# https://wiki.qt.io/PySide_Tutorials


class Terminal(QtGui.QMainWindow):
    """
    Central host to a fleet of :class:`.Pilot` s and user-facing
    :mod:`~.core.gui` objects.

    Called as a module with the -f flag to give the location of a prefs file, eg::

        python terminal.py -f prefs_file.json

    if the -f flag is not passed, looks in the default location for prefs
    (ie. `/usr/rpilot/prefs.json`)

    **Listens used by the internal :class:`.Net_Node` **

    +---------------+--------------------------------+--------------------------------------------------------+
    | Key           | Method                         | Description                                            |
    +===============+================================+========================================================+
    | `'STATE'`     | :meth:`~.Terminal.l_state`     | A Pi has changed state                                 |
    +---------------+--------------------------------+--------------------------------------------------------+
    | `'PING'`      | :meth:`~.Terminal.l_ping`      |  Someone wants to know if we're alive                  |
    +---------------+--------------------------------+--------------------------------------------------------+
    | `'DATA'`      | :meth:`~.Terminal.l_data`      | Receiving data to store                                |
    +---------------+--------------------------------+--------------------------------------------------------+
    | `'HANDSHAKE'` | :meth:`~.Terminal.l_handshake` | Pilot first contact, telling us it's alive and its IP  |
    +---------------+--------------------------------+--------------------------------------------------------+

    ** Prefs needed by Terminal **
    Typically set by :mod:`.setup.setup_terminal`

    * **BASEDIR** - Base directory for all local rpilot data, typically `/usr/rpilot`
    * **MSGPORT** - Port to use for our ROUTER listener, default `5560`
    * **DATADIR** -  `os.path.join(params['BASEDIR'], 'data')`
    * **SOUNDDIR** - `os.path.join(params['BASEDIR'], 'sounds')`
    * **PROTOCOLDIR** - `os.path.join(params['BASEDIR'], 'protocols')`
    * **LOGDIR** - `os.path.join(params['BASEDIR'], 'logs')`
    * **REPODIR** - Path to RPilot git repo
    * **PILOT_DB** - Location of `pilot_db.json` used to populate :attr:`~.Terminal.pilots`

    Attributes:
        node (:class:`~.networking.Net_Node`): Our Net_Node we use to communicate with our main networking object
        networking (:class:`~.networking.Terminal_Station`): Our networking object to communicate with the outside world
        subjects (dict): A dictionary mapping subject ID to :class:`~.subject.Subject` object.
        pilots (dict): A dictionary mapping pilot ID to a list of its subjects, its IP, and any other pilot attributes.
        layout (:class:`QtGui.QGridLayout`): Layout used to organize widgets
        control_panel (:class:`~.gui.Control_Panel`): Control Panel to manage pilots and subjects
        data_panel (:class:`~.plots.Plot_Widget`): Plots for each pilot and subject.
        logo (:class:`QtGui.QLabel`): Label holding our beautiful logo ;X
        logger (:class:`logging.Logger`): Used to log messages and network events.
        log_handler (:class:`logging.FileHandler`): Handler for logging
        log_formatter (:class:`logging.Formatter`): Formats log entries as::

            "%(asctime)s %(levelname)s : %(message)s"

    """

    def __init__(self):
        # type: () -> None
        super(Terminal, self).__init__()

        # networking
        self.node = None
        self.networking = None
        self.heartbeat_dur = 10 # check every n seconds whether our pis are around still

        # data
        self.subjects = {}  # Dict of our open subject objects
        self.pilots = None

        # gui
        self.layout = None
        self.widget = None
        self.file_menu = None
        self.tool_menu = None
        self.control_panel = None
        self.data_panel = None
        self.logo = None


        # logging
        self.logger        = None
        self.log_handler   = None
        self.log_formatter = None

        # Load pilots db as ordered dictionary
        with open(prefs.PILOT_DB) as pilot_file:
            self.pilots = json.load(pilot_file, object_pairs_hook=odict)

        # Start Logging
        self.init_logging()

        # Listen dictionary - which methods to call for different messages
        # Methods are spawned in new threads using handle_message
        self.listens = {
            'STATE': self.l_state, # A Pi has changed state
            'PING' : self.l_ping,  # Someone wants to know if we're alive
            'DATA' : self.l_data,
            'HANDSHAKE': self.l_handshake # a pi is making first contact, telling us its IP
        }

        # Make invoker object to send GUI events back to the main thread
        self.invoker = Invoker()
        prefs.add('INVOKER', self.invoker)

        self.initUI()

        # Start Networking
        # Networking is in two parts,
        # "internal" networking for messages sent to and from the Terminal object itself
        # "external" networking for messages to and from all the other components,
        # The split is so the external networking can run in another process, do potentially time-consuming tasks
        # like resending & confirming message delivery without blocking or missing messages

        self.node = Net_Node(id="_T", upstream='T', port=prefs.MSGPORT, listens=self.listens)
        self.logger.info("Net Node Initialized")

        # Start external communications in own process
        # Has to be after init_network so it makes a new context
        self.networking = Terminal_Station(self.pilots)
        self.networking.start()
        self.logger.info("Station object Initialized")

        # send an initial ping looking for our pilots
        self.node.send('T', 'INIT')

        # start beating ur heart
        self.heartbeat_timer = threading.Timer(self.heartbeat_dur, self.heartbeat)
        self.heartbeat_timer.daemon = True
        self.heartbeat_timer.start()
        #self.heartbeat(once=True)


    def init_logging(self):
        """
        Start logging to a timestamped file in `prefs.LOGDIR`
        """

        timestr = datetime.datetime.now().strftime('%y%m%d_%H%M%S')
        log_file = os.path.join(prefs.LOGDIR, 'Terminal_Log_{}.log'.format(timestr))

        self.logger        = logging.getLogger('main')
        self.log_handler   = logging.FileHandler(log_file)
        self.log_formatter = logging.Formatter("%(asctime)s %(levelname)s : %(message)s")
        self.log_handler.setFormatter(self.log_formatter)
        self.logger.addHandler(self.log_handler)
        self.logger.setLevel(logging.INFO)
        self.logger.info('Terminal Logging Initiated')

    def initUI(self):
        """
        Initializes graphical elements of Terminal.

        Including...

        * Toolbar
        * :class:`.gui.Control_Panel`
        * :class:`.plots.Plot_Widget`
        """

        # set stylesheet for main window
        self.setStyleSheet(styles.TERMINAL)

        # set central widget
        self.widget = QtGui.QWidget()
        self.setCentralWidget(self.widget)



        # Start GUI
        self.layout = QtGui.QGridLayout()
        self.layout.setSpacing(0)
        self.layout.setContentsMargins(0,0,0,0)
        self.widget.setLayout(self.layout)

        self.setWindowTitle('Terminal')
        #self.menuBar().setFixedHeight(40)

        # Main panel layout
        #self.panel_layout.setContentsMargins(0,0,0,0)

        # Init toolbar
        # File menu
        # make menu take up 1/10 of the screen
        winsize = app.desktop().availableGeometry()
        bar_height = (winsize.height()/25)+5

        self.menuBar().setFixedHeight(bar_height)
        #self.menuBar().setStyleSheet('QMenuBar:item {  }')


        self.file_menu = self.menuBar().addMenu("&File")
        self.file_menu.setObjectName("file")
        new_pilot_act = QtGui.QAction("New &Pilot", self, triggered=self.new_pilot)
        new_prot_act  = QtGui.QAction("New Pro&tocol", self, triggered=self.new_protocol)
        #batch_create_subjects = QtGui.QAction("Batch &Create subjects", self, triggered=self.batch_subjects)
        # TODO: Update pis
        self.file_menu.addAction(new_pilot_act)
        self.file_menu.addAction(new_prot_act)
        #self.file_menu.addAction(batch_create_subjects)

        # Tools menu
        self.tool_menu = self.menuBar().addMenu("&Tools")
        subject_weights_act = QtGui.QAction("View Subject &Weights", self, triggered=self.subject_weights)
        update_protocol_act = QtGui.QAction("Update Protocols", self, triggered=self.update_protocols)
        reassign_act = QtGui.QAction("Batch Reassign Protocols", self, triggered=self.reassign_protocols)
        calibrate_act = QtGui.QAction("Calibrate &Water Ports", self, triggered=self.calibrate_ports)
        self.tool_menu.addAction(subject_weights_act)
        self.tool_menu.addAction(update_protocol_act)
        self.tool_menu.addAction(reassign_act)
        self.tool_menu.addAction(calibrate_act)

        # Tests menu
        self.tests_menu = self.menuBar().addMenu("Test&s")
        bandwidth_test_act = QtGui.QAction("Test Bandwidth", self, triggered=self.test_bandwidth)
        self.tests_menu.addAction(bandwidth_test_act)


        ## Init main panels and add to layout
        # Control panel sits on the left, controls pilots & subjects
        self.control_panel = Control_Panel(pilots=self.pilots,
                                           subjects=self.subjects,
                                           start_fn=self.toggle_start)

        # Data panel sits on the right, plots stuff.
        self.data_panel = Plot_Widget()
        self.data_panel.init_plots(self.pilots.keys())



        # Logo goes up top
        # https://stackoverflow.com/questions/25671275/pyside-how-to-set-an-svg-icon-in-qtreewidgets-item-and-change-the-size-of-the


        pixmap_path = os.path.join(os.path.dirname(prefs.REPODIR), 'graphics', 'autopilot_logo_small.svg')
        #svg_renderer = QtSvg.QSvgRenderer(pixmap_path)
        #image = QtGui.QImage()
        #self.logo = QtSvg.QSvgWidget()


        # set size, preserving aspect ratio
        logo_height = round(44.0*((bar_height-5)/44.0))
        logo_width = round(139*((bar_height-5)/44.0))

        svg_renderer = QtSvg.QSvgRenderer(pixmap_path)
        image = QtGui.QImage(logo_width, logo_height, QtGui.QImage.Format_ARGB32)
        # Set the ARGB to 0 to prevent rendering artifacts
        image.fill(0x00000000)
        svg_renderer.render(QtGui.QPainter(image))
        pixmap = QtGui.QPixmap.fromImage(image)
        self.logo = QtGui.QLabel()
        self.logo.setPixmap(pixmap)


        self.menuBar().setCornerWidget(self.logo, QtCore.Qt.TopRightCorner)
        self.menuBar().adjustSize()

        #self.logo.load(pixmap_path)
        # Combine all in main layout
        #self.layout.addWidget(self.logo, 0,0,1,2)
        self.layout.addWidget(self.control_panel, 0,0,1,1)
        self.layout.addWidget(self.data_panel, 0,1,1,1)
        self.layout.setColumnStretch(0, 1)
        self.layout.setColumnStretch(1, 3)

        # Set size of window to be fullscreen without maximization
        # Until a better solution is found, if not set large enough, the pilot tabs will
        # expand into infinity. See the Expandable_Tabs class
        #pdb.set_trace()
        screensize = app.desktop().screenGeometry()
        winsize = app.desktop().availableGeometry()

        # want to subtract bounding title box, our title bar, and logo height.
        # our y offset will be the size of the bounding title box

        # Then our tilebar
        # multiply by three to get the inner (file, etc.) bar, the top bar (min, maximize, etc)
        # and then the very top system tray bar in ubuntu
        #titleBarHeight = self.style().pixelMetric(QtGui.QStyle.PM_TitleBarHeight,
        #                                          QtGui.QStyleOptionTitleBar(), self) * 3
        title_bar_height = screensize.height()-winsize.height()

        #titleBarHeight = bar_height*2
        # finally our logo
        logo_height = bar_height



        winheight = winsize.height() - title_bar_height - logo_height  # also subtract logo height
        winsize.setHeight(winheight)
        self.max_height = winheight
        self.setGeometry(winsize)
        self.setSizePolicy(QtGui.QSizePolicy.Maximum, QtGui.QSizePolicy.Maximum)

        # Set heights on control panel and data panel


        # move to primary display and show maximized
        primary_display = app.desktop().availableGeometry(0)
        self.move(primary_display.left(), primary_display.top())
        # self.resize(primary_display.width(), primary_display.height())
        #
        self.control_panel.setMaximumHeight(winheight)
        self.data_panel.setMaximumHeight(winheight)

        self.show()
        logging.info('UI Initialized')

    def reset_ui(self):
        """
        Clear Layout and call :meth:`~.Terminal.initUI` again
        """

        # type: () -> None
        self.layout = QtGui.QGridLayout()
        self.layout.setSpacing(0)
        self.layout.setContentsMargins(0,0,0,0)
        self.widget.setLayout(self.layout)
        self.setCentralWidget(self.widget)
        self.initUI()


    ##########################3
    # Listens & inter-object methods

    def heartbeat(self, once=False):
        self.node.send('T', 'INIT', repeat=False, flags={'NOREPEAT': True})

        if not once:
            self.heartbeat_timer = threading.Timer(self.heartbeat_dur, self.heartbeat)
            self.heartbeat_timer.daemon = True
            self.heartbeat_timer.start()


    def toggle_start(self, starting, pilot, subject=None):
        """Start or Stop running the currently selected subject's task. Sends a
        message containing the task information to the concerned pilot.

        Each :class:`Pilot_Panel` is given a lambda function that calls this
        one with the arguments specified See :class:`Pilot_Button`, as it is
        what calls this function.

        Args:
            starting (bool): Does this button press mean we are starting (True)
                or stopping (False) the task?
            pilot: Which Pilot is starting or stopping?
            subject: Which Subject is currently selected?
        """
        # stopping is the enemy of starting so we put them in the same function to learn about each other
        if starting is True:
            # Get Weights
            start_weight, ok = QtGui.QInputDialog.getDouble(self, "Set Starting Weight",
                                                            "Starting Weight:")
            if ok:
                # Ope'nr up if she aint
                if subject not in self.subjects.keys():
                    self.subjects[subject] = Subject(subject)

                task = self.subjects[subject].prepare_run()
                task['pilot'] = pilot
                self.subjects[subject].update_weights(start=float(start_weight))

                self.node.send(to=bytes(pilot), key="START", value=task)
                # also let the plot know to start
                self.node.send(to=bytes("P_{}".format(pilot)), key="START", value=task)

            else:
                # pressed cancel, don't start
                return

        else:
            # Get Weights
            stop_weight, ok = QtGui.QInputDialog.getDouble(self, "Set Stopping Weight",
                                                           "Stopping Weight:")
            
            if ok:
                # Send message to pilot to stop running,
                # it should initiate a coherence checking routine to make sure
                # its data matches what the Terminal got,
                # so the terminal will handle closing the subject object
                self.node.send(to=bytes(pilot), key="STOP")
                # also let the plot know to start
                self.node.send(to=bytes("P_{}".format(pilot)), key="STOP")
                # TODO: Start coherence checking ritual
                # TODO: Auto-select the next subject in the list.

                self.subjects[subject].stop_run()
                self.subjects[subject].update_weights(stop=float(stop_weight))

            else:
                # pressed cancel
                return


    ############################
    # MESSAGE HANDLING METHODS

    def l_data(self, value):
        """
        A Pilot has sent us data.

        `value` field of message should have `subject` and `pilot` added to dictionary for identification.

        Any key in `value` that matches a column in the subject's trial data table will be saved.

        If the subject graduates after receiving this piece of data, stop the current
        task running on the Pilot and send the new one.

        Args:
            value (dict): A dict of field-value pairs to save
        """
        # A Pi has sent us data, let's save it huh?
        subject_name = value['subject']
        self.subjects[subject_name].save_data(value)
        if self.subjects[subject_name].did_graduate.is_set() is True:
            self.node.send(to=value['pilot'], key="STOP", value={'graduation':True})
            self.subjects[subject_name].stop_run()
            self.subjects[subject_name].graduate()
            task = self.subjects[subject_name].prepare_run()
            task['pilot'] = value['pilot']

            self.node.send(to=value['pilot'], key="START", value=task)

    def l_ping(self, value):
        """
        TODO:
            Reminder to implement heartbeating.

        Note:
            Currently unused, as Terminal Net_Node stability hasn't been
            a problem and no universal system of heartbeating has been
            established (global stability has not been an issue).

        Args:
            value: (unused)
        """
        # Only our Station object should ever ping us, because
        # we otherwise want it handling any pings on our behalf.

        # self.send_message('ALIVE', value=b'T')
        pass



    def l_state(self, value):
        """A Pilot has changed state, keep track of it.

        Args:
            value (dict): dict containing `state` .
        """
        # TODO: If we are stopping, we enter into a cohere state
        # TODO: If we are stopped, close the subject object.
        # TODO: Also tell the relevant dataview to clear

        # update the pilot button
        if value['pilot'] in self.pilots.keys():
            if 'state' not in self.pilots[value['pilot']].keys():
                self.pilots[value['pilot']]['state'] = value['state']
                self.control_panel.panels[value['pilot']].button.set_state(value['state'])
            elif value['state'] != self.pilots[value['pilot']]['state']:
                self.control_panel.panels[value['pilot']].button.set_state(value['state'])
                self.pilots[value['pilot']]['state'] = value['state']

            




    def l_handshake(self, value):
        """
        Pilot is sending its IP and state on startup.

        If we haven't heard of this pilot before, make a new entry in :attr:`~.Terminal.pilots`
        and :meth:`.gui.Control_Panel.update_db` .

        Args:
            value (dict): dict containing `ip` and `state`
        """
        if value['pilot'] in self.pilots.keys():
            if 'ip' in value.keys():
                self.pilots[value['pilot']]['ip'] = value['ip']
            if 'state' in value.keys():
                self.pilots[value['pilot']]['state'] = value['state']

        else:
            self.new_pilot(name=value['pilot'], ip=value['ip'])

        # update the pilot button
        if value['pilot'] in self.control_panel.panels.keys():
            self.control_panel.panels[value['pilot']].button.set_state(value['state'])


        self.control_panel.update_db()

    #############################
    # GUI & etc. methods

    def new_pilot(self, ip='', name=None):
        """
        Make a new entry in :attr:`.Terminal.pilots` and make appropriate
        GUI elements.

        Args:
            ip (str): Optional. if given, stored in db.
            name (str): If None, prompted for a name, otherwise used for entry in pilot DB.
        """
        if name is None:
            name, ok = QtGui.QInputDialog.getText(self, "Pilot ID", "Pilot ID:")

        # make sure we won't overwrite ourself
        if name in self.pilots.keys():
            # TODO: Pop a window confirming we want to overwrite
            pass

        if name != '':
            new_pilot = {name:{'subjects':[], 'ip':ip}}
            self.control_panel.update_db(new=new_pilot)
            self.reset_ui()
        else:
            # Idk maybe pop a dialog window but i don't really see why
            pass

    def new_protocol(self):
        """
        Open a :class:`.gui.Protocol_Wizard` to create a new protocol.

        Prompts for name of protocol, then saves in `prefs.PROTOCOLDIR`
        """
        self.new_protocol_window = Protocol_Wizard()
        self.new_protocol_window.exec_()

        if self.new_protocol_window.result() == 1:
            steps = self.new_protocol_window.steps

            # The values useful to the step functions are stored with a 'value' key in the param_dict
            save_steps = []
            for s in steps:
                param_values = {}
                for k, v in s.items():
                    if 'value' in v.keys():
                        param_values[k] = v['value']
                    elif k == 'stim':
                        # TODO: Super hacky - don't do this. Refactor params already.
                        param_values[k] = {}
                        for stimtype, stim in v.items():
                            param_values[k][stimtype] = stim
                save_steps.append(param_values)

            # Name the protocol
            name, ok = QtGui.QInputDialog.getText(self, "Name Protocol", "Protocol Name:")
            if ok and name != '':
                protocol_file = os.path.join(prefs.PROTOCOLDIR, name + '.json')
                with open(protocol_file, 'w') as pfile_open:
                    json.dump(save_steps, pfile_open, indent=4, separators=(',', ': '), sort_keys=True)
            elif name == '' or not ok:
                placeholder_name = 'protocol_created_{}'.format(datetime.date.today().isoformat())
                protocol_file = os.path.join(prefs.PROTOCOLDIR, placeholder_name + '.json')
                with open(protocol_file, 'w') as pfile_open:
                    json.dump(save_steps, pfile_open, indent=4, separators=(',', ': '), sort_keys=True)

    def list_subjects(self):
        """
        Get a list of all subject IDs

        Returns:
            list: list of all subject IDs present in :attr:`.Terminal.pilots`
        """
        subjects = []
        for pilot, vals in self.pilots.items():
            subjects.extend(vals['subjects'])
        return subjects

    def subject_weights(self):
        """
        Gets recent weights from all :attr:`~.Terminal.subjects` and
        open a :class:`.gui.Weights` window to view or set weights.
        """
        subjects = self.list_subjects()

        # open objects if not already
        for subject in subjects:
            if subject not in self.subjects.keys():
                self.subjects[subject] = Subject(subject)

        # for each subject, get weight
        weights = []
        for subject in subjects:
            weight = self.subjects[subject].get_weight(include_baseline=True)
            weight['subject'] = subject
            weights.append(weight)

        self.weight_widget = Weights(weights, self.subjects)
        self.weight_widget.show()

    def update_protocols(self):
        """
        If we change the protocol file, update the stored version in subject files
        """
        #
        # get list of protocol files
        protocols = os.listdir(prefs.PROTOCOLDIR)
        protocols = [p for p in protocols if p.endswith('.json')]


        subjects = self.list_subjects()
        for subject in subjects:
            if subject not in self.subjects.keys():
                self.subjects[subject] = Subject(subject)

            protocol_bool = [self.subjects[subject].protocol_name == p.rstrip('.json') for p in protocols]
            if any(protocol_bool):
                which_prot = np.where(protocol_bool)[0][0]
                protocol = protocols[which_prot]
                self.subjects[subject].assign_protocol(os.path.join(prefs.PROTOCOLDIR, protocol), step_n=self.subjects[subject].step)

        msgbox = QtGui.QMessageBox()
        msgbox.setText("Subject Protocols Updated")
        msgbox.exec_()

    def reassign_protocols(self):
        """
        Batch reassign protocols and steps.

        Opens a :class:`.gui.Reassign` window after getting protocol data,
        and applies any changes made in the window.
        """
        # get list of protocol files
        protocols = os.listdir(prefs.PROTOCOLDIR)
        protocols = [os.path.splitext(p)[0] for p in protocols if p.endswith('.json')]

        # get subjects and current protocols
        subjects = self.list_subjects()
        subjects_protocols = {}
        for subject in subjects:
            if subject not in self.subjects.keys():
                self.subjects[subject] = Subject(subject)

            subjects_protocols[subject] = [self.subjects[subject].protocol_name, self.subjects[subject].step]

        reassign_window = Reassign(subjects_protocols, protocols)
        reassign_window.exec_()

        if reassign_window.result() == 1:
            subject_protocols = reassign_window.subjects

            for subject, protocol in subject_protocols.items():
                step = protocol[1]
                protocol = protocol[0]

                # since assign_protocol also changes the step, stash the step number here to tell if it's changed
                subject_orig_step = self.subjects[subject].step



                if self.subjects[subject].protocol_name != protocol:
                    self.logger.info('Setting {} protocol from {} to {}'.format(subject, self.subjects[subject].protocol_name, protocol))
                    protocol_file = os.path.join(prefs.PROTOCOLDIR, protocol + '.json')
                    self.subjects[subject].assign_protocol(protocol_file, step)

                if subject_orig_step != step:
                    self.logger.info('Setting {} step from {} to {}'.format(subject, subject_orig_step, step))
                    step_name = self.subjects[subject].current[step]['step_name']
                    #update history also flushes current - aka it also actually changes the step number
                    self.subjects[subject].update_history('step', step_name, step)

    def calibrate_ports(self):

        calibrate_window = Calibrate_Water(self.pilots)
        calibrate_window.exec_()

        if calibrate_window.result() == 1:
            for pilot, p_widget in calibrate_window.pilot_widgets.items():
                p_results = p_widget.volumes
                # p_results are [port][dur] = {params} so running the same duration will
                # overwrite a previous run. unnest here so pi can keep a record
                unnested_results = {}
                for port, result in p_results.items():
                    unnested_results[port] = []
                    # result is [dur] = {params}
                    for dur, inner_result in result.items():
                        inner_result['dur'] = dur
                        unnested_results[port].append(inner_result)

                # send to pi
                self.node.send(to=pilot, key="CALIBRATE_RESULT",
                               value = unnested_results)

            msgbox = QtGui.QMessageBox()
            msgbox.setText("Calibration results sent!")
            msgbox.exec_()

    def test_bandwidth(self):
        # turn off logging while we run
        self.networking.set_logging(False)
        self.node.do_logging.clear()

        bandwidth_test = Bandwidth_Test(self.pilots)
        bandwidth_test.exec_()

        self.networking.set_logging(True)
        self.node.do_logging.set()






    def closeEvent(self, event):
        """
        When Closing the Terminal Window, close any running subject objects,
        'KILL' our networking object.

        Since the `:class:`.Net_Node` keeping us alive is a `daemon`, no need
        to explicitly kill it.

        """
        # TODO: Check if any subjects are currently running, pop dialog asking if we want to stop

        # Close all subjects files
        for m in self.subjects.values():
            if m.running is True:
                m.stop_run()

        # Stop networking
        # send message to kill networking process
        self.node.send(key="KILL")

if __name__ == "__main__":

    sys.path.append(prefs.REPODIR)

    #with open(prefs_file) as prefs_file_open:
    #    prefs = json.load(prefs_file_open)

    app = QtGui.QApplication(sys.argv)
    app.setStyle('plastique') # Keeps some GTK errors at bay
    ex = Terminal()
    sys.exit(app.exec_())

