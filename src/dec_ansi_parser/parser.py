"""Terminal control sequence parser, following https://www.vt100.net/emu/dec_ansi_parser"""

from __future__ import annotations

import codecs
import errno
import io
from enum import Enum
from typing import (
    IO,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    TypeVar,
    Union,
    cast,
)

__all__ = ["State", "Action", "Parameters", "Parser"]


# anywhere is denoted by None in the table generation code
class State(Enum):
    ground = 0
    escape = 1
    escape_intermediate = 2
    csi_entry = 3
    csi_param = 4
    csi_intermediate = 5
    csi_ignore = 6
    dcs_entry = 7
    dcs_param = 8
    dcs_intermediate = 9
    dcs_passthrough = 10
    dcs_ignore = 11
    osc_string = 12
    # sos/pm/apc string
    other_string = 13

    def __str__(self) -> str:
        return self.name.upper()


ground = State.ground
escape = State.escape
escape_intermediate = State.escape_intermediate
csi_entry = State.csi_entry
csi_param = State.csi_param
csi_intermediate = State.csi_intermediate
csi_ignore = State.csi_ignore
dcs_entry = State.dcs_entry
dcs_param = State.dcs_param
dcs_intermediate = State.dcs_intermediate
dcs_passthrough = State.dcs_passthrough
dcs_ignore = State.dcs_ignore
osc_string = State.osc_string
# sos/pm/apc string
other_string = State.other_string


class Action(Enum):
    ignore = 0
    print = 1
    execute = 2
    clear = 3
    collect = 4
    param = 5
    esc_dispatch = 6
    csi_dispatch = 7
    hook = 8
    put = 9
    unhook = 10
    osc_start = 11
    osc_put = 12
    osc_end = 13


class Transition(NamedTuple):
    # if None, will stay in current state
    target: Optional[State]
    action: Optional[Action]


def do(action: Action) -> Transition:
    return Transition(target=None, action=action)


def to(state: State, action: Optional[Action] = None) -> Transition:
    return Transition(target=state, action=action)


class myslice(NamedTuple):
    start: int
    stop: int


class Indexer:
    # pylint: disable=too-few-public-methods
    def __getitem__(
        self, key: Union[int, slice, Tuple[Union[int, slice], ...]]
    ) -> Tuple[myslice, ...]:
        if not isinstance(key, tuple):
            key = (key,)
        return tuple(
            myslice(x.start, x.stop) if isinstance(x, slice) else myslice(x, x)
            for x in key
        )


r = Indexer()

# work around https://github.com/python/mypy/issues/7907 (waiting on mypy 0.920; see #11158)
RangeTransitions = Dict[Tuple[myslice, ...], Transition]

S = State
A = Action
# these will override any conflicting transitions in other states
anywhere_table: RangeTransitions = {
    # CAN, SUB
    r[0x18, 0x1A]: to(ground, A.execute),
    # ESC
    r[0x1B]: to(escape),
    # C1 (8-bit) controls:
    r[0x90]: to(dcs_entry),
    r[0x9B]: to(csi_entry),
    r[0x9D]: to(osc_string),
    # SOS, PM, APC
    r[0x98, 0x9E, 0x9F]: to(other_string),
    # ST
    r[0x9C]: to(ground, A.ignore),
    # all other undefined C1 controls
    r[0x80:0x8F, 0x91:0x97, 0x99, 0x9A]: to(ground, A.execute),
}
r_normal_c0 = r[0x00:0x17, 0x19, 0x1C:0x1F]

range_table: Dict[State, RangeTransitions] = {
    ground: {r_normal_c0: do(A.execute), r[0x20:0x7F]: do(A.print)},
    escape: {
        r_normal_c0: do(A.execute),
        r[0x20:0x2F]: to(escape_intermediate, A.collect),
        r[0x30:0x4F, 0x51:0x57, 0x59, 0x5A, 0x5C, 0x60:0x7E]: to(
            ground, A.esc_dispatch
        ),
        r[0x50]: to(dcs_entry),
        r[0x5B]: to(csi_entry),
        r[0x5D]: to(osc_string),
        r[0x58, 0x5E, 0x5F]: to(other_string),
        r[0x7F]: do(A.ignore),
    },
    escape_intermediate: {
        r_normal_c0: do(A.execute),
        r[0x20:0x2F]: do(A.collect),
        r[0x30:0x7E]: to(ground, A.esc_dispatch),
        r[0x7F]: do(A.ignore),
    },
    csi_entry: {
        r_normal_c0: do(A.execute),
        r[0x20:0x2F]: to(csi_intermediate, A.collect),
        r[0x30:0x39, 0x3B]: to(csi_param, A.param),
        # sub-parameters
        r[0x3A]: to(csi_param, A.param),
        r[0x3C:0x3F]: to(csi_param, A.collect),
        r[0x40:0x7E]: to(ground, A.csi_dispatch),
        r[0x7F]: do(A.ignore),
    },
    csi_param: {
        r_normal_c0: do(A.execute),
        r[0x20:0x2F]: to(csi_intermediate, A.collect),
        r[0x30:0x39, 0x3B]: do(A.param),
        # sub-parameters
        r[0x3A]: do(A.param),
        r[0x3C:0x3F]: to(csi_ignore),
        r[0x40:0x7E]: to(ground, A.csi_dispatch),
        r[0x7F]: do(A.ignore),
    },
    csi_intermediate: {
        r_normal_c0: do(A.execute),
        r[0x20:0x2F]: do(A.collect),
        r[0x30:0x3F]: to(csi_ignore),
        r[0x40:0x7E]: to(ground, A.csi_dispatch),
        r[0x7F]: do(A.ignore),
    },
    csi_ignore: {
        r_normal_c0: do(A.execute),
        r[0x20:0x3F, 0x7F]: do(A.ignore),
        r[0x40:0x7E]: to(ground),
    },
    dcs_entry: {
        r_normal_c0: do(A.ignore),
        r[0x20:0x2F]: to(dcs_intermediate, A.collect),
        r[0x30:0x39, 0x3B]: to(dcs_param, A.param),
        r[0x3A]: to(dcs_ignore),
        r[0x3C:0x3F]: to(dcs_param, A.collect),
        r[0x40:0x7E]: to(dcs_passthrough),
        r[0x7F]: do(A.ignore),
    },
    dcs_param: {
        r_normal_c0: do(A.ignore),
        r[0x20:0x2F]: to(dcs_intermediate, A.collect),
        r[0x30:0x39, 0x3B]: do(A.param),
        r[0x3A, 0x3C:0x3F]: to(dcs_ignore),
        r[0x40:0x7E]: to(dcs_passthrough),
        r[0x7F]: do(A.ignore),
    },
    dcs_intermediate: {
        r_normal_c0: do(A.ignore),
        r[0x20:0x2F]: do(A.collect),
        r[0x30:0x3F]: to(dcs_ignore),
        r[0x40:0x7E]: to(dcs_passthrough),
        r[0x7F]: do(A.ignore),
    },
    dcs_passthrough: {
        r_normal_c0: do(A.put),
        r[0x20:0x7E]: do(A.put),
        # NB: this should not put
        # r[0x9C]: to(ground),
        r[0x7F]: do(A.ignore),
    },
    dcs_ignore: {
        r_normal_c0: do(A.ignore),
        r[0x20:0x7F]: do(A.ignore),
        # r[0x9C]: to(ground),
    },
    osc_string: {
        r_normal_c0: do(A.ignore),
        r[0x20:0x7F]: do(A.osc_put),
        # r[0x9C]: to(ground),
        # XTerm accepts BEL (0x07) as an OSC string terminator:
        r[0x07]: to(ground, A.ignore),
    },
    other_string: {
        r_normal_c0: do(A.ignore),
        r[0x20:0x7F]: do(A.ignore),
        # r[0x9C]: to(ground),
    },
}

on_entry: Dict[State, Action] = {
    escape: A.clear,
    csi_entry: A.clear,
    dcs_entry: A.clear,
    dcs_passthrough: A.hook,
    osc_string: A.osc_start,
}

on_exit: Dict[State, Action] = {
    dcs_passthrough: A.unhook,
    osc_string: A.osc_end,
}


def expand_table(
    state_table: Dict[State, RangeTransitions], anywhere: RangeTransitions
) -> Dict[State, List[Transition]]:
    # transition lists must be dense on 00-9F (A0-FF are wrapped to 20-7F)
    t: Dict[State, List[Transition]] = {}
    placeholder = Transition(None, None)

    def store_transitions(
        _curr_state: State, l: List[Transition], rt: RangeTransitions
    ) -> None:
        for slices, trans in rt.items():
            for s in slices:
                for i in range(s.start, s.stop + 1, 1):
                    # if l[i] is not placeholder:
                    #     print(
                    #         f"overriding transition in {curr_state} at 0x{i:02X}: {l[i]} -> {trans}"
                    #     )
                    l[i] = trans

    for state, transitions in state_table.items():
        l = [placeholder] * (0x9F + 1)
        store_transitions(state, l, transitions)
        # override transitions from anywhere
        store_transitions(state, l, anywhere)
        for i, trans in enumerate(l):
            if trans is placeholder:
                print(f"missing transition in {state} at 0x{i:02X}")
        t[state] = l

    return t


state_transitions = expand_table(range_table, anywhere_table)

T = TypeVar("T")


class Parameters(List[Union[Optional[int], List[Optional[int]]]]):
    def get(self, key: int, *, default: Union[int, T]) -> Union[int, T]:
        try:
            val = self[key]
        except IndexError:
            return default
        if isinstance(val, list):
            val = val[0] if val else None
        if val is None:
            return default
        return val

    def __bool__(self) -> bool:
        return len(self) != 0 and self != [None]


def try_unicode(
    stream: Union[IO[bytes], io.RawIOBase], raw_bytes: bytearray
) -> Iterator[int]:
    """Yield a stream of character codes, with the sign denoting whether they
    were parsed as raw ASCII or a multibyte UTF-8 sequence (positive if ASCII,
    negative if UTF-8).
    All bytes read are appended to `raw_bytes`.
    """
    Decoder = codecs.getincrementaldecoder("utf-8")
    while True:
        try:
            c = stream.read(1)
        except OSError as e:
            if e.errno != errno.EIO:
                raise
            break  # EIO means EOF on some systems
        if not c:
            break
        raw_bytes.extend(c)
        if 0xC2 <= c[0] <= 0xF4:
            # valid UTF-8 start byte
            try:
                decoder = Decoder()
                output = decoder.decode(c)
                while not output:
                    # read() only returns None if it's non-blocking, which shouldn't
                    # be the case here
                    chars = cast(bytes, stream.read(1))
                    raw_bytes.extend(chars)
                    output = decoder.decode(chars)
                yield -ord(output)
            except UnicodeDecodeError as ex:
                # print(traceback.format_exc())
                for x in ex.object:
                    yield x
        else:
            yield c[0]


class Parser:
    def __init__(
        self,
        callback: Callable[[Parser, Optional[Action], int], None],
        debug: bool = False,
    ):
        """Callback will be called with None as the action when the parser is reset and at EOF."""
        self._trans = state_transitions
        self._cb = callback
        self._enable_debug = debug

        self.state = State.ground
        self.intermediate = ""
        # None is used for an unspecified parameter (vtparse assumes 0)
        self.parameters = Parameters([None])
        # used to suppress an esc_dispatch after a 2-byte string terminator
        self.esc_ended_string = False
        # the raw bytes read since the last time callback was called, for reconstruction
        self.curr_raw = bytearray()

    def reset(self) -> None:
        self.state = State.ground
        self.esc_ended_string = False
        self._cb(self, None, -1)
        self.curr_raw.clear()
        self.clear()

    def clear(self) -> None:
        self.intermediate = ""
        self.parameters = Parameters([None])

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self._enable_debug:
            print(msg.format(*args), **kwargs)

    def parse(self, data: Union[IO[bytes], io.RawIOBase]) -> None:
        for char in try_unicode(data, self.curr_raw):
            trans_table = self._trans[self.state]
            if char < 0:
                # unicode character
                new_state, action = trans_table[0x7E]
                char = -char
            else:
                new_state, action = trans_table[
                    char & 0x7F if 0xA0 <= char <= 0xFF else char
                ]

            if new_state is not None:
                if self.state in on_exit:
                    self.debug("processing on_exit from {}", self.state)
                    self.process(on_exit[self.state])
                    if self.state in (S.osc_string, S.dcs_passthrough) and char == 0x1B:
                        self.esc_ended_string = True
                if action is not None:
                    self.debug("processing action with state change")
                    self.process(action, char)
                if new_state in on_entry:
                    self.debug("processing on_entry to {}", new_state)
                    self.process(on_entry[new_state])
                self.state = new_state
            elif action is not None:
                self.debug("processing action")
                self.process(action, char)
        self._cb(self, None, -1)
        self.curr_raw.clear()

    def process(
        self,
        action: Action,
        char: int = -1,
        # optimization: avoid several dict lookups in Action per call by storing
        # these as local parameters when the function is defined
        # Saves ~20% on a 1.3MB file with lots of truecolor escape sequences
        ignore: Action = Action.ignore,
        clear: Action = Action.clear,
        collect: Action = Action.collect,
        param: Action = Action.param,
    ) -> None:
        if action is ignore:
            pass
        elif action is clear:
            self.clear()
        elif action is collect:
            self.intermediate += chr(char)
        elif action is param:
            assert char >= 0
            if chr(char) == ";":
                self.parameters.append(None)
            elif chr(char) == ":":
                # handle subparameters, from section 5.4.2 of ECMA-48
                if isinstance(self.parameters[-1], list):
                    self.parameters[-1].append(None)
                else:
                    self.parameters[-1] = [
                        self.parameters[-1],
                        None,
                    ]
            else:
                assert len(self.parameters) != 0, "parameters should not be empty"
                lst: Union[Parameters, List[Optional[int]]]
                if isinstance(self.parameters[-1], list):
                    lst = self.parameters[-1]
                else:
                    lst = self.parameters
                assert lst[-1] is None or isinstance(lst[-1], int)
                digit = char - ord("0")
                # use or here to handle None neatly
                lst[-1] = (lst[-1] or 0) * 10 + digit
        else:
            if self.esc_ended_string:
                self.esc_ended_string = False
                if action is A.esc_dispatch and chr(char) == "\\":
                    return
            self._cb(self, action, char)
            self.curr_raw.clear()
