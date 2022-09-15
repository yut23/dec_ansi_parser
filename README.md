# dec_ansi_parser

Pure-python terminal control sequence parser, based on [Paul Williams' DEC-compatible parser](https://www.vt100.net/emu/dec_ansi_parser).

Changes from Williams' parser:

* Handles subparameters
* Allows UTF-8 encoded strings (treated as normal text)
* Ignores the backslash (0x5C) in a 7-bit string terminator when exiting an OSC or DCS control string

Any invalid UTF-8 sequences are parsed as individual raw bytes.

## Installation

```bash
$ pip install dec_ansi_parser
```

## Usage

- TODO

## Contributing

Interested in contributing? Check out the contributing guidelines. Please note that this project is released with a Code of Conduct. By contributing to this project, you agree to abide by its terms.

## License

`dec_ansi_parser` was created by yut23. It is licensed under the terms of the BSD 3-Clause license.

## Credits

`dec_ansi_parser` was created with [`cookiecutter`](https://cookiecutter.readthedocs.io/en/latest/) and the `py-pkgs-cookiecutter` [template](https://github.com/py-pkgs/py-pkgs-cookiecutter).
