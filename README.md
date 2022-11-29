# Soundbrowser

*Soundbrowser* is a gstreamer based sound player. It is intended to be
a simple, fast and useful sound browser tool. Most sound player
softwares are music players, and their user interface is designed such
that users need to load files (into a playlist). Often they are also
centered around the concept of a sound library. *Soundbrowser* on the
other hand is designed to be able to quickly browse through the
filesystem and directly play the sounds while navigating. There is no
concepts of playlist nor sound library, it is intended to browse very
quickly through big collections of sounds or musics on the filesystem,
listen to them, and show useful metadata about these sounds.

## Motivations and goals

* browse filesystem with one-click playing of sound files

* to be used as soundbank browser while producing music

* to be used as music browser while preparing a DJ mix

* no "library" to keep in sync with filesystem

* display useful sound metadata, in particular BPM and Keys

* listen and display the most up to date version of files (no need to
  explicitely reload a file if it has changed on disk)

* handle as much as possible different fileformats

* fast and user-friendly user-interface

NON-goals:

* fancy user-interface

* advanced features

## Dependencies

* gstreamer, qt, python3, pyside2

* should work on any gstreamer and qt supported platform (linux, unix,
  macos, windows...)

* list of debian bullseye linux package dependencies: python3-yaml
  python3-schema python3-pyside2.qtcore python3-pyside2.qtgui
  python3-pyside2.qtwidgets python3-gst-1.0 gir1.2-gtk-3.0
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good
  gstreamer1.0-plugins-ugly

## Build and install

Build a single embedded `soundbrowser` file:

    $ make dist
