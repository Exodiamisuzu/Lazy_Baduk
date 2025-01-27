import pexpect
from functools import wraps
import threading

import kivy

from os import path
from itertools import product

import os
import stat

os.environ['LD_LIBRARY_PATH'] = path.abspath('./')

MOVE_TIME_S = 5
MOVE_TIME_S = 10


def choose_lz_binary(board_size):
    cur_dir = path.dirname(__file__)

    if kivy.platform == 'android':
        leelaz_binary = 'leelaz_binary_android'
    #elif kivy.platform == 'linux':
    #   leelaz_binary = '/home/exodia/Desktop/cloneproj/LazyBaduk/leelaz'
    else:
        leelaz_binary ='/home/exodia/Desktop/cloneproj/LazyBaduk/leelaz'
        #leelaz_binary = 'leelaz_binary_x86_64'

    assert board_size in (9, 13, 19)  # only sizes supported for now

    if board_size == 9:
        leelaz_binary += '_9x9'
    elif board_size == 13:
        leelaz_binary += '_13x13'

    leelaz_binary = path.join(cur_dir, leelaz_binary)

    assert path.exists(leelaz_binary)
    print('Found LZ binary {}'.format(leelaz_binary))

    # Make sure the binary is executable (in a hacky way for now)
    st = os.stat(leelaz_binary)
    print('current stat is', st)
    os.chmod(leelaz_binary, st.st_mode | stat.S_IEXEC | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.chmod(leelaz_binary, 33261)

    return leelaz_binary
    


class LeelaZeroWrapper(object):

    def __init__(self, board_size=19):

        self.leelaz_binary = choose_lz_binary(board_size)
        self.board_size = board_size

        self.pondering = False
        self.process = None

        self.current_analysis = []

        self.next_to_play = 'b'

        # LZ data to be read from the process
        self.lz_name = None
        self.lz_version = None
        self.lz_output = []  # list of output lines
        self.lz_up_to_date = True

        # bounds for the rectangle we should search
        self.bottom_left_analysis_coord = (0, 0)
        self.top_right_analysis_coord = (18, 18)

        self.command_number = 1
        self.command_queue = []
        self.commands_awaiting_response = {}

        self.lz_generating_move = False
        self.lz_move_to_play = None

        self.connect_to_leela_zero()

        self.begin_reading()

    def begin_reading(self):
        self.read_thread = threading.Thread(
            target=self.read,
            name='lz-thread')

        self.read_thread.start()

    def send_command(self, command):
        """Add a command to the queue, it will not be sent to LZ immediately."""
        self.command_queue.append(command)

        # If not already waiting for something, send the new command
        if self.lz_up_to_date:
            self.send_command_from_queue()

    def send_command_from_queue(self):
        """Pop the first command from the queue, and send it to LZ."""
        if not self.command_queue:
            return

        # skip redundant analysis commands
        while True:
            command = self.command_queue.pop(0)
            if command.startswith('lz-analyze') and any([c.startswith('lz-analyze') for c in self.command_queue]):
                # skip this command, it is redundant
                continue
            break

        # reset the lz_generating_move boolean if necessary
        if command.startswith('genmove'):
            self.lz_generating_move = True

        self.send_command_to_leelaz(command)
        
    def send_command_to_leelaz(self, command):
        """Send a command to the LZ process, tagged with a number so we can get its output."""
        command_string = '{number} {command}'.format(number=self.command_number,
                                                     command=command)
        self.commands_awaiting_response[self.command_number] = command
        self.command_number += 1

        self.lz_up_to_date = False

        self.process.sendline(command_string)

    def read(self):
        while True:
            if not self.process.isalive():
                print('LZ process is not alive, stopping read')
                print('Remaining lines to read were:')
                for line in self.process.readlines():
                    print('  ' + line.decode('utf-8'))
                break  # if the LZ process ended, stop reading from it

            line = self.process.readline()
            self.parse_line(line.decode('utf-8'))

    def parse_line(self, line):
        """Read and interpret a line of output from the LZ process"""
        printed_line = line.strip()
        if len(line) > 80:
            printed_line = line[:77] + '...'
        print('$ LZ> {}'.format(printed_line))

        if line.startswith('info'):
            self.parse_lz_analysis(line)

        elif ' -> ' in line:
            # parse best move info
            pass

        elif line.startswith('play'):
            # interpret an LZ move
            pass

        elif line.startswith('=') or line.startswith('?'):
            # this line is a response to a command we sent
            # line has the format "=$NUM $RESPONSE"
            number = int(line.strip().split(' ')[0][1:])

            if len(line.strip().split(' ')) == 1:
                response = ''
            else:
                response = ' '.join(line.strip().split(' ')[1:])

            # we are ready to send the next command
            self.send_command_from_queue()

            self.handle_command_response(number, response)

        # also add the line to our log
        if line.strip():
            self.lz_output.append(line.strip())
        
    def parse_lz_analysis(self, line):
        moves = line.split('info')
        moves = [m.strip() for m in moves][1:]

        current_analysis = [self.parse_lz_analysis_move(m) for m in moves]

        # Remove passes from the move list, as they aren't handled properly by the gui yet
        current_analysis = [m for m in current_analysis if not m.is_pass]
        # Sort the moves to guarantee that they are in order of most to fewest visits
        # This apparently wasn't necessary in older LZ versions, but seems to be now
        self.current_analysis = sorted(current_analysis, key=lambda move: -move.visits)

        if not self.current_analysis:
            # stop here if there is no analysis, e.g. if all the moves are pass
            return

        # Add relative values to the analysis
        max_visits = max([move.visits for move in self.current_analysis])
        for move in self.current_analysis:
            move.relative_visits = move.visits / max_visits

    def parse_lz_analysis_move(self, move):
        return MoveAnalysis(move)

    def consume_move_if_available(self):
        if self.lz_move_to_play is not None:
            move = self.lz_move_to_play
            self.lz_move_to_play = None
            return move

    def handle_command_response(self, number, response):
        command = self.commands_awaiting_response.pop(number)

        if command.startswith('lz-analyze'):
            self.pondering = True
            self.current_analysis = []
        else:
            self.pondering = False
        if command == 'version':
            self.lz_version = response
        elif command == 'name':
            self.lz_name = response
        elif command.startswith('genmove'):
            self.lz_move_to_play = response
            self.lz_generating_move = False
            self.current_analysis = []
        else:
            print('Nothing to do with response "{}" to command "{}"'.format(response, command))

        if number == (self.command_number - 1):
            self.lz_up_to_date = True
        else:
            self.lz_up_to_date = False

    def is_ready(self):
        """Returns True if the LZ process is alive, and has finished initialising."""
        return self.process.isalive() and all([self.lz_name is not None,
                                               self.lz_version is not None])

    def play_move(self, colour, coordinates):
        last_command = self.command_queue[-1] if self.command_queue else ''

        colour_string = 'black' if colour.startswith('b') else 'white'

        self.send_command('play {colour} {coordinates}'.format(
            colour=colour_string,
            coordinates=coordinates))

        print('!!', self.pondering, last_command)
        if self.pondering or last_command.startswith('lz-analyze'):
            self.send_lz_analyse()

        self.current_analysis = []

    def undo_move(self):
        last_command = self.command_queue[-1] if self.command_queue else ''
        self.send_command('undo')

        if self.pondering or last_command.startswith('lz-analyze'):
            self.send_lz_analyse()

        self.current_analysis = []

    def send_lz_analyse(self):
        bottom_left = self.bottom_left_analysis_coord
        bl_x, bl_y = bottom_left
        top_right = self.top_right_analysis_coord
        tr_x, tr_y = top_right
        allowed_region_coords = ','.join(
            [numeric_coordinates_to_alphanumeric_coordinates(c)
             for c in product(range(bl_x, tr_x + 1), range(bl_y, tr_y + 1))])
        self.send_command('lz-analyze {} 25 allow b {} 1 allow w {} 1'.format(
            self.next_to_play,
            allowed_region_coords,
            allowed_region_coords))

    def generate_move(self, colour):
        colour_string = 'black' if colour.startswith('b') else 'white'

        self.lz_generating_move = True

        self.send_command('time_settings 0 {} 1'.format(MOVE_TIME_S))
        self.send_command('genmove {}'.format(colour_string))
        self.send_command('name')

    def set_next_colour_to_play(self, colour):
        self.next_to_play = colour

    def toggle_ponder(self, active):
        if not active and self.pondering:
            self.send_command('name')  # sending a command cancels the pondering

        elif active and not self.pondering:
            self.send_lz_analyse()

    def restart_ponder(self):
        """Stop and restart pondering, to take account of any parameter changes."""
        if not self.pondering:
            return False

        self.send_command('name')  # sending a command cancels the pondering
        self.send_lz_analyse()


    def connect_to_leela_zero(self):
        if self.process is not None:
            return

        print('ready to connect to LZ')
        if self.board_size == 19:
            network = 'network.gz'
            #network = 'd351f06e446ba10697bfd2977b4be52c3de148032865eaaf9efc9796aea95a0c.gz'  # 15x192
            network = 'd351f06e446ba10697bfd2977b4be52c3de148032865eaaf9efc9796aea95a0c.gz'
            # network = '33986b7f9456660c0877b1fc9b310fc2d4e9ba6aa9cee5e5d242bd7b2fb1b166.gz'  # 20x256
            # network = '85a936847e2759ab5ea0389bbe061245dc6025ef9d317a0d1315cc1078b0c34a.gz'  # 40x256
            #network = 'elfv2.gz'  # 20x256?
            #network = '0a963117.gz'
        elif self.board_size == 13:
            network = '13_205.txt.gz'
        elif self.board_size == 9:
            network = 'leelaz9x9/9x9-20-128.txt.gz'
            network = 'leelaz9x9/152s.txt.gz'
        else:
            raise ValueError('No weights known for board size {}'.format(self.board_size))
        self.process = pexpect.spawn(
            '{} --gtp --lagbuffer 0 --weights {} --resignpct 0 --cpu-only'.format(self.leelaz_binary, network),
            timeout=None)
        print('self.process is {}, alive {}'.format(self.process, self.process.isalive()))
        assert self.process.isalive()

        self.send_command('name')
        self.send_command('version')

    def kill(self):
        self.process.kill(9)


class MoveAnalysis(object):
    def __init__(self, move):
        move_info = self
        words = move.split(' ')

        word = words.pop(0)
        assert word == 'move'

        word = words.pop(0)
        self.lz_coordinates = word

        word = words.pop(0)
        assert word == 'visits'

        word = words.pop(0)
        self.visits = int(word)

        word = words.pop(0)
        assert word == 'winrate'

        word = words.pop(0)
        self.winrate = float(word) / 100.0

        word = words.pop(0)
        assert word == 'prior'

        word = words.pop(0)
        self.prior = int(word)

        # lcb was introduced in the LZ release around April 2019
        word = words.pop(0)
        if word == 'lcb':
            word = words.pop(0)
            self.lcb = int(word)

            word = words.pop(0)
        else:
            self.lcb = None

        assert word == 'order'

        word = words.pop(0)
        self.order = int(word)

        word = words.pop(0)
        assert word == 'pv'

        move_sequence = words
        self.move_sequence = move_sequence
        self.relative_visits = 0  # must be set elsewhere

        print(self.lz_coordinates, 'move_sequence is', move_sequence)
        
    @property
    def numeric_coordinate_sequence(self):
        return [lz_coordinates_to_numeric_coordinates(lz_coords) for lz_coords in self.move_sequence]

    @property
    def is_pass(self):
        return self.lz_coordinates == 'pass'

    @property
    def numeric_coordinates(self):
        if self.is_pass:
            return None

        return lz_coordinates_to_numeric_coordinates(self.lz_coordinates)

    @property
    def alphanumeric_coordinates(self):
        """The alphanumeric coordinates of the move according to the lzviewer
        reference, not LZ's own coordinate system."""
        return numeric_coordinates_to_alphanumeric_coordinates(self.numeric_coordinates)

def lz_coordinates_to_numeric_coordinates(coords):
    letter = coords[0]
    number = coords[1:]

    assert ord('A') <= ord(letter) <= ord('Z')
    horiz_coord = ord(letter) - ord('A')
    if horiz_coord > 8:
        horiz_coord -= 1  # correct for absence of I from coordinates
    vert_coord = int(number)
    vert_coord -= 1  # convert from 1-indexed to 0-indexed

    return (horiz_coord, vert_coord)

def numeric_coordinates_to_alphanumeric_coordinates(coords):
    number_coord, letter_coord = coords

    letter_ord = ord('A') + letter_coord
    if letter_ord >= ord('I'):
        letter_ord += 1
    letter = chr(letter_ord)

    number = number_coord + 1

    return '{}{}'.format(letter, number)
