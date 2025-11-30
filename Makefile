all: lib/ui_lib/main_win.py lib/ui_lib/prefs_dial.py lib/ui_lib/help_dial.py lib/ui_lib/soundbrowser_rc.py

lib/ui_lib/%.py: lib/ui_lib/%.ui lib/ui_lib/soundbrowser_rc.py
	qtchooser -run-tool=uic -qt=5 $< -g python --from-imports > $@

lib/ui_lib/soundbrowser_rc.py: lib/ui_lib/soundbrowser.qrc lib/ui_lib/icons/adwaita-icon-theme-3.38.0/Adwaita/64x64/*/*.png lib/ui_lib/fonts/DigitalNumbers-Regular/*.ttf
	qtchooser -run-tool=rcc -qt=5 $< -g python > $@

clean:
	rm -rf lib/ui_lib/main_win.py lib/ui_lib/prefs_dial.py lib/ui_lib/help_dial.py lib/ui_lib/soundbrowser_rc.py build/ soundbrowser soundbrowser.zip
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name '*.pyc' -exec rm -rf {} +

dist: clean all
	rsync -am --delete --include='*.py' --exclude='build/' --include='*/' --exclude='*' . build/
	mv build/soundbrowser.py build/__main__.py
	cd build && zip -0r ../soundbrowser.zip *
	echo '#!/usr/bin/env python3' | cat - soundbrowser.zip > soundbrowser
	rm soundbrowser.zip
	chmod a+x soundbrowser

design:
	lib/ui_lib/designer
