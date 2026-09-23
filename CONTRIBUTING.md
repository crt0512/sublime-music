# Contributing

This is a personal fork of [Sublime Music](https://github.com/sublime-music/sublime-music),
kept alive after upstream reached end of maintenance. 

Issues and pull requests are welcome, but expect a hobbyists response time.

## Development setup

You'll need GTK3, PyGObject and libmpv from your distro. 
(on Debian/Ubuntu:`python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-notify-0.7 libmpv2`), then:

```
make venv       # .venv with the dev and test tools, on top of the system PyGObject
make run        # run from the source tree; ARGS="-m debug" for verbose logging
make test       # pytest (unit tests and doctests)
make lint       # black --check, isort --check-only, flake8, mypy
make format     # black and isort
```

`make help` lists every target, including the packaging ones (`deb`, `install`,
`uninstall`, `build`, `pkg`).

## Code style

The code is formatted with :
[black](https://github.com/psf/black) (line length 99) and
[isort](https://pycqa.github.io/isort/), linted with flake8 
(with the annotations, bugbear, comprehensions, pep3101 and print plugins aswell as being type-checked with mypy)

`make lint` runs all of them and use `logging` instead of `print`.

## Reporting bugs

Run the app with `-m debug` (or `make run ARGS="-m debug"`) and include the relevant log
output, the server software you use (Gonic, Navidrome, whatever else) and the steps to reproduce.

## Simulating bad network conditions

`REQUEST_DELAY=3,5 make run` adds a random 3 to 5 second delay to every request.
