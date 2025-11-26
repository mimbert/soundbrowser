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

## Screenshots

Main Window
![Main Window Screenshot](/screenshots/main_window.webp?raw=true "Main Window")

Configuration Dialog
![Configuration Dialog Screenshot](/screenshots/config_dialog.webp?raw=true "Configuration Dialog")

## Usage

* if a sound has changed on disk, it detects and reloads it

* keyboard:

  * up / down arrows : navigate sounds

  * ENTER : play

  * spacebar : toggle play/pause

  * ESC : stop

  * ctrl-c : copy path of current file

  * ctrl-v : go to path pasted from clipboard if possible

  * l : toggle looping

  * h : toggle display of hidden files

  * f : toggle filtering of files

  * m : toggle display of metadata pane

  * mouse right-click : context menu to reload a sound

## Command line options

* help or debug flags

* can pass a directory or file path on the command line to directly
  open that directory / file

## Configuration

Configuration options in the config dialog:

* autoplay on keyboard move toggle

* autoplay on mouse click toggle

* UI dark theme toggle

* startup directory modes (current dir, home dir, last dir, fixed dir)

* file extension filter: which files are displayed when file filtering
  is activated (based on extensions)

* gstreamer output sink. Can be used to select between pulse, jack,
  etc. Output sink properties can be used, Eg. to select connect mode
  and port names for jack. See:
  https://gstreamer.freedesktop.org/documentation/jack/jackaudiosink.html

the configuration is stored is `~/.soundbrowser.conf.yaml`. Most of
the fields can be edited manually if needed, provided you don't mess
with the format. if a field is missing, it will be added. If a field
is unknown, it will cause an error. Note that *Soundbrowser* writes this
file on exit, so if you need to edit it, quit *Soundbrowser* before.

## TODO

* fix seek pos at stop (depending on stop cause -> 0 or force 100%)

* add counter and reorg slider ui

* qt6

## Motivations and goals

* browse filesystem with one-click playing of sound files

* to be used as soundbank browser while producing music

* to be used as music browser while preparing a DJ mix

* no "library" to keep in sync with filesystem

* display useful sound metadata, in particular BPM and Keys

* listen and display the most up to date version of files (no need to
  explicitly reload a file if it has changed on disk)

* handle as much as possible different fileformats

* fast and user-friendly user-interface

NON-goals:

* fancy user-interface

* advanced features

## License

MIT license

## Dependencies

* gstreamer, qt, python3, pyside2

* should work on any gstreamer and qt supported platform (linux, unix,
  macos, windows...)

* list of debian bullseye linux package dependencies: python3-yaml
  python3-schema python3-pyside2.qtcore python3-pyside2.qtgui
  python3-pyside2.qtwidgets python3-gst-1.0 gir1.2-gtk-3.0
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good
  gstreamer1.0-plugins-ugly

* to build: qtchooser

## Build and install

Compile the qt resources:

    $ make

Build a single embedded `soundbrowser` file:

    $ make dist
