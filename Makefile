all: ui/main_win.py

ui/main_win.py: ui/main_win.ui soundbrowser_rc.py
	qtchooser -run-tool=uic -qt=5 $< -g python > $@

soundbrowser_rc.py: ui/soundbrowser.qrc
	qtchooser -run-tool=rcc -qt=5 $< -g python > $@

clean:
	rm -rf ui/main_win.py soundbrowser_rc.py
