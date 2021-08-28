all: ui/soundbrowser.py

ui/soundbrowser.py: ui/soundbrowser.ui
	qtchooser -run-tool=uic -qt=5 ui/soundbrowser.ui -g python > ui/soundbrowser.py
