all: ui/main_win.py ui/prefs_dial.py

ui/%.py: ui/%.ui soundbrowser_rc.py
	qtchooser -run-tool=uic -qt=5 $< -g python > $@

soundbrowser_rc.py: ui/soundbrowser.qrc
	qtchooser -run-tool=rcc -qt=5 $< -g python > $@

clean:
	rm -rf ui/main_win.py soundbrowser_rc.py build/ soundbrowser soundbrowser.zip

dist:
	mkdir -p build/ui
	cp *.py build/
	mv build/soundbrowser.py build/__main__.py
	cp ui/*.py ui/*.png build/ui/
	cd build && zip ../soundbrowser.zip * ui/*
	echo '#!/usr/bin/env python3' | cat - soundbrowser.zip > soundbrowser
	rm soundbrowser.zip
	chmod a+x soundbrowser
