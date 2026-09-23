"""The program PyInstaller freezes into the macOS bundle's executable."""
import multiprocessing

if __name__ == "__main__":
    # The Chromecast file server runs in a child process; in a frozen app that child
    # would otherwise start the whole application again. This must happen before
    # importing the app, because PyInstaller's multiprocessing child process starts
    # by running this entry point.
    multiprocessing.freeze_support()

    from sublime_music.__main__ import main

    main()
