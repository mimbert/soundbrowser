all: lib/ui_lib/main_win.py lib/ui_lib/prefs_dial.py lib/ui_lib/soundbrowser_rc.py

lib/ui_lib/%.py: lib/ui_lib/%.ui lib/ui_lib/soundbrowser_rc.py
	qtchooser -run-tool=uic -qt=5 $< -g python --from-imports > $@

lib/ui_lib/soundbrowser_rc.py: lib/ui_lib/soundbrowser.qrc lib/ui_lib/*.png
	qtchooser -run-tool=rcc -qt=5 $< -g python > $@


clean:
	rm -rf lib/ui_lib/main_win.py lib/ui_lib/prefs_dial.py lib/ui_lib/soundbrowser_rc.py build/ soundbrowser soundbrowser.zip
	find . -type d -name __pycache__ -exec rm -rf {} +

dist: all
	rsync -am --delete --include='*.py' --include='*.png' --exclude='build/' --include='*/' --exclude='*' . build/
	mv build/soundbrowser.py build/__main__.py
	cd build && zip -r ../soundbrowser.zip *
	echo '#!/usr/bin/env python3' | cat - soundbrowser.zip > soundbrowser
	rm soundbrowser.zip
	chmod a+x soundbrowser

design:
	lib/ui_lib/designer