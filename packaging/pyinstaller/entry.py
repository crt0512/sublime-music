"""The program PyInstaller freezes into the macOS bundle's executable."""
import multiprocessing

from sublime_music.__main__ import main

if __name__ == "__main__":
    # The Chromecast file server runs in a child process; in a frozen app that child
    # would otherwise start the whole application again.
    multiprocessing.freeze_support()
    main()
