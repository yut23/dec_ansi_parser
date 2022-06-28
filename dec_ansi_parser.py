#!/usr/bin/env python3
"""Terminal control sequence parser, following https://www.vt100.net/emu/dec_ansi_parser"""

import argparse
import codecs
import sys
from enum import Enum, auto
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


# anywhere is denoted by None in the table generation code
class State(Enum):
    ground = auto()
    escape = auto()
    escape_intermediate = auto()
    csi_entry = auto()
    csi_param = auto()
    csi_intermediate = auto()
    csi_ignore = auto()
    dcs_entry = auto()
    dcs_param = auto()
    dcs_intermediate = auto()
    dcs_passthrough = auto()
    dcs_ignore = auto()
    osc_string = auto()
    # sos/pm/apc string
    other_string = auto()


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
    ignore = auto()
    print = auto()
    execute = auto()
    clear = auto()
    collect = auto()
    param = auto()
    esc_dispatch = auto()
    csi_dispatch = auto()
    hook = auto()
    put = auto()
    unhook = auto()
    osc_start = auto()
    osc_put = auto()
    osc_end = auto()


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


def try_unicode(stream: IO[bytes]) -> Iterator[Tuple[int, bool]]:
    "Yield a character code and whether the character was encoded as UTF-8."
    Decoder = codecs.getincrementaldecoder("utf-8")
    while c := stream.read(1):
        if 0xC2 <= c[0] <= 0xF4:
            # valid UTF-8 start byte
            try:
                decoder = Decoder()
                output = decoder.decode(c)
                while not output:
                    output = decoder.decode(stream.read(1))
                yield ord(output), True
            except UnicodeDecodeError as ex:
                # print(traceback.format_exc())
                for x in ex.object:
                    yield x, False
        else:
            yield c[0], False


class Parser:
    def __init__(
        self, callback: Callable[["Parser", Action, int], None], debug: bool = False
    ):
        self._trans = state_transitions
        self._cb = callback
        self._enable_debug = debug

        self.state = State.ground
        self.intermediate = ""
        # None is used for an unspecified parameter (vtparse assumes 0)
        self.parameters = Parameters([None])
        # used to suppress an esc_dispatch after a 2-byte string terminator
        self.esc_ended_string = False

    def reset(self) -> None:
        self.state = State.ground
        self.esc_ended_string = False
        self.clear()

    def clear(self) -> None:
        self.intermediate = ""
        self.parameters = Parameters([None])

    def debug(self, *args: Any, **kwargs: Any) -> None:
        if self._enable_debug:
            print(*args, **kwargs)

    def parse(self, data: IO[bytes]) -> None:
        for char, was_utf8 in try_unicode(data):
            # print(f"got: {char=}, {was_utf8=}")
            trans_table = self._trans[self.state]
            if was_utf8:
                # unicode character
                new_state, action = trans_table[0x7E]
            else:
                new_state, action = trans_table[
                    char & 0x7F if 0xA0 <= char <= 0xFF else char
                ]

            if new_state is not None:
                if self.state in on_exit:
                    self.debug(f"processing on_exit from {self.state.name.upper()}")
                    self.process(on_exit[self.state])
                    if self.state in (S.osc_string, S.dcs_passthrough) and char == 0x1B:
                        self.esc_ended_string = True
                if action is not None:
                    self.debug("processing action with state change")
                    self.process(action, char)
                if new_state in on_entry:
                    self.debug(f"processing on_entry to {new_state.name.upper()}")
                    self.process(on_entry[new_state])
                self.state = new_state
            elif action is not None:
                self.debug("processing action")
                self.process(action, char)

    def process(self, action: Action, char: int = -1) -> None:
        if action is A.ignore:
            pass
        elif action is A.clear:
            self.clear()
        elif action is A.collect:
            self.intermediate += chr(char)
        elif action is A.param:
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
                lst: List[Union[Optional[int], List[Optional[int]]]]
                if isinstance(self.parameters[-1], list):
                    lst = self.parameters[-1]  # type: ignore
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


_UNKNOWN_TAG = "[[[UNKNOWN]]]"
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


def describe_generic(parser: Parser, char: int) -> None:
    if char != -1:
        print(f"  Char: 0x{char:02x} ('{chr(char)}')")
    if parser.intermediate:
        print(f"  {len(parser.intermediate)} Intermediate chars:")
        for c in parser.intermediate:
            print(f"    0x{ord(c):02x} ('{c}')")
    if parser.parameters:
        print(f"  {len(parser.parameters)} Parameters:")
        for p in parser.parameters:
            print(f"    {p:d}")


def describe_exec(char: int) -> None:
    control_chars = {
        0x07: "BEL",
        0x08: "BS",
        0x09: "TAB",
        0x0A: "LF",
        0x0B: "VT",
        0x0C: "FF",
        0x0D: "CR",
    }
    if char in control_chars:
        print(f"Execute {control_chars[char]} ({repr(chr(char))}, 0x{char:02x})")
    else:
        print(f"Execute {repr(chr(char))} (0x{char:02x}) {_UNKNOWN_TAG}")


def describe_esc(parser: Parser, char_: int) -> None:
    assert char_ >= 0
    char = chr(char_)
    if char == "=":
        print("Enter Application Keypad mode (DECKPAM)")
    elif char == ">":
        print("Enter Normal Keypad mode (DECKPNM)")
    elif char == "7":
        print("Save cursor position (DECSC)")
    elif char == "8":
        print("Restore cursor position (DECRC)")
    elif parser.intermediate and parser.intermediate[0] in "()*+":
        # 94-character sets
        code = parser.intermediate[1:] + char
        charset = {
            "A": "UK",
            "B": "ASCII",
            "C": "Finnish",
            "5": "Finnish",
            "H": "Swedish",
            "7": "Swedish",
            "K": "German",
            "Q": "French Canadian",
            "9": "French Canadian",
            "R": "French",
            "f": "French",
            "Y": "Italian",
            "Z": "Spanish",
            "4": "Dutch",
            '">': "Greek",
            "%2": "Turkish",
            "%6": "Portuguese",
            "%=": "Hebrew",
            "=": "Swiss",
            "`": "Norwegian/Danish",
            "E": "Norwegian/Danish",
            "6": "Norwegian/Danish",
            "0": "DEC Special Character and Line Drawing Set",
            "<": "DEC Supplemental",
            ">": "DEC Technical",
            '"4': "DEC Hebrew",
            '"?': "DEC Greek",
            "%0": "DEC Turkish",
            "%5": "DEC Supplemental Graphics",
            "&4": "DEC Cyrillic",
        }.get(code, f"unknown ({code!r}) {_UNKNOWN_TAG}")
        element = {"(": "G0", ")": "G1", "*": "G2", "+": "G3"}[parser.intermediate[0]]
        print(f"Set {element} character set to {charset}")
    elif parser.intermediate and parser.intermediate[0] in "-./":
        # 96-character sets
        charset = {
            "A": "Latin-1 Supplemental",
            "B": "Latin-2 Supplemental",
            "F": "Greek Supplemental",
            "H": "Hebrew Supplemental",
            "L": "Latin-Cyrillic",
            "M": "Latin-5 Supplemental",
        }.get(char, f"unknown ({char}) {_UNKNOWN_TAG}")
        element = {"-": "G1", ".": "G2", "/": "G3"}[parser.intermediate[0]]
        print(f"Set {element} character set to ISO {charset}")
    else:
        describe_unknown("ESC", parser, char_)


def describe_sgr(params: List[Union[Optional[int], List[Optional[int]]]]) -> None:
    unknown_message = f"Unknown SGR sequence: {params} {_UNKNOWN_TAG}"
    while params:
        p = params.pop(0)
        if isinstance(p, list):
            p, *subparams = p
        else:
            subparams = []
        info: Dict[str, str] = {"type": "unknown"}
        set_attrs = {
            1: "bold",
            2: "faint",
            3: "italic",
            4: "underline",
            5: "blink",
            6: "blink",
            7: "inverse",
            8: "invisible",
            9: "strikethrough",
            21: "double underline",
        }
        reset_attrs = {
            22: "bold/faint",
            23: "italic",
            24: "underline",
            25: "blink",
            27: "inverse",
            28: "invisible",
            29: "strikethrough",
        }
        if p in set_attrs:
            info["type"] = "set"
            info["attribute"] = set_attrs[p]
        elif p in reset_attrs:
            info["type"] = "reset"
            info["attribute"] = reset_attrs[p]
        elif p == 0 or p is None:
            info["type"] = "custom"
            info["message"] = "Reset all SGR attributes"
        elif p in (38, 48, 58):
            if not subparams:
                try:
                    if isinstance(params[0], list):
                        # special form handled by xterm
                        subparams = cast(List[Optional[int]], params.pop(0))
                    else:
                        subparams = [cast(int, params.pop(0))]
                        if subparams[0] == 2:
                            # direct color needs 3 RGB values
                            # move the first three values from params to the end of subparams
                            if not all(isinstance(x, int) for x in params[:3]):
                                raise TypeError()
                            subparams.extend(cast(List[int], params[:3]))
                            params[:3] = []
                        elif subparams[0] == 5:
                            # 256 color needs 1 palette index
                            if not isinstance(params[0], int):
                                raise TypeError()
                            subparams.append(cast(int, params.pop(0)))
                except (IndexError, TypeError):
                    print(unknown_message)
                    continue
            # now we can use subparams as normal
            info["type"] = "color"
            info["which"] = {38: "foreground", 48: "background", 58: "decoration"}[p]
            p1 = subparams[0]
            if p1 == 2:
                color = "#" + "".join(f"{c:02x}" for c in subparams[1:])
            elif p1 == 5:
                color = f"256-color index {subparams[1]}"
            # only change the foreground color for colorized output
            esc_color = "\033[0;38:{}m".format(":".join(map(str, subparams)))
            info["color"] = f"{esc_color}{color}\033[0m"
        elif any(p in range(n, n + 8) for n in [30, 40, 90, 100]):
            assert isinstance(p, int)
            # standard 8 colors, plus bright variants
            info["type"] = "color"
            color = {
                0: "black",
                1: "red",
                2: "green",
                3: "yellow",
                4: "blue",
                5: "magenta",
                6: "cyan",
                7: "gray",
            }[p % 10]
            if p // 10 in (3, 9):
                info["which"] = "foreground"
                esc_color = f"\033[0;{p}m"
            else:
                info["which"] = "background"
                esc_color = f"\033[0;{p - 10}m"
            if p >= 90:
                color = f"bright {color}"
            info["color"] = f"{esc_color}{color}\033[0m"
        elif p in (39, 49, 59):
            info["type"] = "reset color"
            info["which"] = {39: "foreground", 49: "background", 59: "decoration"}[p]

        # display the attribute info
        format_strings = {
            "set": "Set {attribute}",
            "reset": "Reset {attribute}",
            "color": "Set {which} color to {color}",
            "reset color": "Reset {which} color",
            "custom": "{message}",
        }
        print(format_strings.get(info["type"], unknown_message).format(**info))


def maybe_plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    if count == 1:
        return singular
    if plural is None:
        plural = singular + "s"
    return plural


ANSI_MODES = {
    2: "Disable Keyboard Input (KAM)",
    3: "Display Control Characters",
    4: "Insert Mode (IRM)",
    12: "Send/receive (SRM)",
    20: "Automatic Newline (LNM)",
}
PRIVATE_MODES = {
    1: "Application Cursor Keys (DECCKM)",
    2: "Designate USASCII for character sets G0-G3 (DECANM)",
    3: "132 Column Mode (DECCOLM)",
    4: "Smooth (Slow) Scroll (DECSCLM)",
    5: "Reverse Video (DECSCNM)",
    6: "Origin Mode (DECOM)",
    7: "Auto-Wrap Mode (DECAWM)",
    8: "Auto-Repeat Keys (DECARM)",
    9: "Send Mouse X & Y on button press (X10 protocol)",
    10: "Show toolbar",
    12: "Start blinking cursor",
    13: "Start blinking cursor",
    14: "Enable XOR of blinking cursor control sequence and menu",
    18: "Print Form Feed (DECPFF)",
    19: "Set print extent to full screen (DECPEX)",
    25: "Show cursor (DECTCEM)",
    30: "Show scrollbar",
    35: "Enable font-shifting functions",
    38: "Enter Tektronix mode (DECTEK)",
    40: "Allow 80 -> 132 mode",
    41: "XTerm more(1) fix",
    42: "Enable National Replacement Character sets (DECNRCM)",
    43: "Enable Graphics Expanded Print Mode (DECGEPM)",
    44: "Turn on margin bell",
    # 44: "Enable Graphics Print Color Mode (DECGPCM)",
    45: "Reverse-wraparound mode",
    # 45: "Enable Graphics Print ColorSpace (DECGPCS)",
    46: "Start logging",
    47: "Use Alternate Screen Buffer",
    # 47: "Enable Graphics Rotated Print Mode (DECGRPM)",
    66: "Application keypad mode (DECNKM)",
    67: "Backarrow key sends backspace (DECBKM)",
    69: "Enable left and right margin mode (DECLRMM)",
    80: "Disable Sixel Scrolling (DECSDM)",
    95: "Do not clear screen when DECCOLM is set/reset (DECNCSM)",
    1000: "Send Mouse X & Y on button press and release (X11 protocol)",
    1001: "Use Hilite Mouse Tracking",
    1002: "Use Cell Motion Mouse Tracking",
    1003: "Use All Motion Mouse Tracking",
    1004: "Send FocusIn/FocusOut events",
    1005: "Enable UTF-8 Mouse Mode",
    1006: "Enable SGR Mouse Mode",
    1007: "Enable Alternate Scroll Mode",
    1010: "Scroll to bottom on tty output",
    1011: "Scroll to bottom on key press",
    1015: "Enable urxvt Mouse Mode",
    1016: "Enable SGR Mouse PixelMode",
    1034: 'Interpret "meta" key',
    1035: "Enable special modifiers for Alt and NumLock keys",
    1036: "Send ESC when Meta modifies a key",
    1037: "Send DEL from the editing-keypad Delete key",
    1039: "Send ESC when Alt modifies a key",
    1040: "Keep selection even if not highlighted",
    1041: "Use the CLIPBOARD selection",
    1042: "Enable Urgency window manager hint when Control-G is received",
    1043: "Enable raising of the window when Control-G is received",
    1044: "Reuse the most recent data copied to CLIPBOARD",
    1046: "Enable switching to/from Alternate Screen Buffer",
    1047: "Use Alternate Screen Buffer",
    1048: "Save cursor",
    1049: "Save cursor and switch to the Alternate Screen Buffer",
    1050: "Set terminfo/termcap function-key mode",
    1051: "Set Sun function-key mode",
    1052: "Set HP function-key mode",
    1053: "Set SCO function-key mode",
    1060: "Set legacy keyboard emulation, i.e, X11R6",
    1061: "Set VT220 keyboard emulation",
    2004: "Set bracketed paste mode",
    # mintty settings (see https://github.com/mintty/mintty/wiki/CtrlSeqs)
    7727: "Enable application escape key mode (mintty)",
}


def describe_csi(parser: Parser, char_: int) -> None:
    assert char_ >= 0
    char = chr(char_)
    intermediate = parser.intermediate
    params = parser.parameters
    if not intermediate:
        if char in "ABCD":
            direction = {"A": "up", "B": "down", "C": "forward", "D": "backward"}[char]
            count = params.get(0, default=1)
            thing = maybe_plural(count, "line" if direction in "AB" else "column")
            print(f"Cursor {direction} {count} {thing} (CSI {char})")
        elif char in "G`":
            col = params.get(0, default=1)
            print(f"Move cursor to column {col} (CSI {char})")
        elif char in "Hf":
            row = params.get(0, default=1)
            col = params.get(1, default=1)
            print(f"Move cursor to row {row}, column {col} (CSI {char})")
        elif char == "J":
            desc = {
                0: "Erase display below current line",
                1: "Erase display above current line",
                2: "Erase entire display",
                3: "Erase scroll-back",
            }[params.get(0, default=0)]
            print(f"{desc} (CSI J)")
        elif char == "K":
            desc = {
                0: "Erase line right of cursor",
                1: "Erase line left of cursor",
                2: "Erase line",
            }[params.get(0, default=0)]
            print(f"{desc} (CSI K)")
        elif char in "LM":
            action = {"L": "Insert", "M": "Delete"}[char]
            count = params.get(0, default=1)
            print(f"{action} {count} {maybe_plural(count, 'line')} (CSI {char})")
        elif char in "ST":
            direction = {"S": "up", "T": "down"}[char]
            count = params.get(0, default=1)
            print(
                f"Scroll {direction} {count} {maybe_plural(count, 'line')} (CSI {char})"
            )
        elif char == "X":
            count = params.get(0, default=1)
            print(f"Erase {count} {maybe_plural(count, 'character')} right (CSI X)")
        elif char == "d":
            row = params.get(0, default=1)
            print(f"Move cursor to row {row} (CSI d)")
        elif char == "m":
            describe_sgr(params)
        elif char == "n":
            if params[0] == 5:
                print("Request device status report (CSI n)")
            elif params[0] == 6:
                print("Request cursor position report (CSI n)")
        elif char == "r":
            top = params.get(0, default=1)
            bottom = params.get(1, default="bottom")
            if top == 1 and bottom == "bottom":
                print("Reset scrolling region (CSI r)")
            else:
                print(f"Set scrolling region to rows {top}-{bottom} (CSI r)")
        elif char == "s":
            left = params.get(0, default=1)
            right: Union[int, str] = params.get(1, default=0)
            if right == 0:
                right = "end"
            print(f"Set margin to columns {left}-{right} (CSI s)")
        elif char == "t" and params.get(0, default=0) in (22, 23):
            action = {22: "Push", 23: "Pop"}[params.get(0, default=0)]
            what = {0: "icon and title", 1: "icon", 2: "title"}[
                params.get(1, default=0)
            ]
            print(f"{action} terminal window {what} (CSI t)")
        else:
            describe_unknown("CSI", parser, char_)
    elif intermediate in ("", "?") and char in "hl":
        if not intermediate:
            # set/reset mode (SM/RM)
            action = {"h": "Set mode", "l": "Reset mode"}[char] + f" (CSI {char})"
            modes = ANSI_MODES
        elif intermediate == "?":
            # DEC private mode set/reset (DECSET/DECRST)
            action = {"h": "Set private mode", "l": "Reset private mode"}[char]
            modes = PRIVATE_MODES
        desc_parts = []
        for param in params:
            assert not isinstance(param, list)
            if param is not None:
                desc_parts.append(
                    modes.get(param, f"Unknown mode parameter {param} {_UNKNOWN_TAG}")
                )

        action = f"{action}: ".ljust(20)
        if not desc_parts:
            print(f"{action} No parameters")
        elif len(desc_parts) == 1:
            print(f"{action} {desc_parts[0]}")
        if len(desc_parts) > 1:
            print(f"{action}")
            print("  " + "\n  ".join(desc_parts))
    elif intermediate == ">" and char == "m":
        opt_name = {
            0: "modifyKeyboard",
            1: "modifyCursorKeys",
            2: "modifyFunctionKeys",
            4: "modifyOtherKeys",
        }[params.get(0, default=0)]
        if not params:
            print("Reset all xterm key modifier options")
        elif len(params) == 1 or params[1] is None:
            print(f"Reset xterm {opt_name} option")
        else:
            print(f"Set xterm {opt_name} option to {params[1]}")
    elif intermediate in ("$", "?$") and char == "p":
        if intermediate == "$":
            desc = "mode"
            mode = ANSI_MODES[params.get(0, default=0)]
        elif intermediate == "?$":
            desc = "private mode"
            mode = PRIVATE_MODES[params.get(0, default=0)]
        action = f"Get {desc}:".ljust(20)
        print(f"{action} {mode}")
    elif intermediate == " " and char == "q":
        # select cursor style (DECSCUSR)
        desc = {
            0: "default",
            1: "blinking block",
            2: "steady block",
            3: "blinking underline",
            4: "steady underline",
            5: "blinking bar",
            6: "steady bar",
        }[params.get(0, default=0)]
        print(f"Change cursor to {desc}")
    elif intermediate in ("", ">", "=") and char == "c" and not params:
        version = {"": "primary", ">": "secondary", "=": "tertiary"}[intermediate]
        seq = f"CSI {intermediate + ' ' if intermediate else ''}{char}"
        print(f"Get {version} device attributes ({seq})")
    elif intermediate == ">" and char == "q":
        print("Request xterm name and version")
    else:
        describe_unknown("CSI", parser, char_)


def describe_unknown(name: str, parser: Parser, char: int) -> None:
    if parser.intermediate:
        intermediates = " ".join({" ": "SP"}.get(x, x) for x in parser.intermediate)
        desc = f"{intermediates} {chr(char)}"
    else:
        desc = chr(char)
    print(f"Received {name} {desc} {_UNKNOWN_TAG}")
    if parser.parameters:
        print(f"  Parameters: {parser.parameters}")


last_action: Optional[Action] = None


def print_raw(char: int) -> None:
    "Print a character to stdout, properly interleaved with normal print calls."
    if char > 0x7F:
        data = chr(char).encode()
    else:
        data = bytes([char])
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def parser_callback(parser: Parser, action: Action, char: int) -> None:
    global last_action
    # if (
    #     last_action is not None
    #     and action == A.esc_dispatch
    #     and char == ord("\\")
    #     and last_action in (A.unhook, A.osc_end)
    # ):
    #     # 2-byte ST; ignore
    #     return
    if (
        last_action is not None
        and last_action != action
        and {last_action, action} != {A.esc_dispatch, A.csi_dispatch}
        and (last_action, action) not in ((A.hook, A.put), (A.osc_start, A.osc_put))
    ):
        # add extra newline between different action types
        if last_action is A.print:
            print('"')
        print()

    # print
    if action is A.print:
        if last_action is not A.print:
            print('> "', end="")
        print_raw(char)

    # execute
    if action is A.execute:
        describe_exec(char)

    # esc_dispatch
    # csi_dispatch
    if action is A.esc_dispatch:
        describe_esc(parser, char)
    if action is A.csi_dispatch:
        describe_csi(parser, char)

    # hook
    if action is A.hook:
        print("Start DCS hook:")
        describe_generic(parser, char)
    # put
    if action is A.put:
        print_raw(char)
    # unhook
    if action is A.unhook:
        print("End DCS hook")

    # osc_start
    if action is A.osc_start:
        print("Start OSC handler:")
    # osc_put
    if action is A.osc_put:
        print_raw(char)
    # osc_end
    if action is A.osc_end:
        print("End OSC handler")

    last_action = action


def main() -> None:
    def vtparse_callback(parser: Parser, action: Action, char: int) -> None:
        global last_action
        print(f"Current state: {parser.state.name.upper()}")
        if last_action is not None:
            print(
                f"Received action {action.name.upper()} (last_action={last_action.name.upper()})"
            )
        else:
            print(f"Received action {action.name.upper()}")
        if char != -1:
            print(f"Char: 0x{char:02x} ('{chr(char)}')")
        if parser.intermediate:
            print(f"{len(parser.intermediate)} Intermediate chars:")
            for c in parser.intermediate:
                print(f"  0x{ord(c):02x} ('{c}')")
        if parser.parameters:
            print(f"{len(parser.parameters)} Parameters:")
            for p in parser.parameters:
                print(f"\t{p}")
        print()
        last_action = action

    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--vtparse", action="store_true", help="emulate vtparse_test")
    ap.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="include extra debugging output (only works with --vtparse)",
    )
    ap.add_argument(
        "-s",
        "--script",
        action="store_true",
        help="skip inline metadata in a script(1) output file",
    )
    ap.add_argument(
        "file",
        nargs="?",
        default="-",
        type=argparse.FileType("rb"),
        help="the file to parse",
    )
    args = ap.parse_args()
    if args.vtparse:
        cb = vtparse_callback
    else:
        cb = parser_callback
    parser_ = Parser(cb, debug=args.debug and args.vtparse)
    infile = args.file
    # work around argparse bug (https://github.com/python/cpython/pull/13165)
    if hasattr(infile, "buffer"):
        infile = infile.buffer
    parser_.parse(infile)


if __name__ == "__main__":
    main()
