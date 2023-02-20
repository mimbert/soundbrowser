# coding: utf-8
Vagrant.configure("2") do |config|

  config.vm.synced_folder ".", "/vagrant", type: "rsync", rsync__exclude: [".git/" , ".#*" ]
  config.ssh.forward_agent = true
  config.ssh.forward_x11 = true
  apt = "DEBIAN_FRONTEND=noninteractive apt-get -q -y"
  config.vm.provider :virtualbox do |vb|
    vb.customize ["modifyvm", :id, '--audio', 'pulse', '--audioin', 'on', '--audioout', 'on', '--audiocontroller', 'ac97'] # choices: hda sb16 ac97
  end

  config.vm.define "debstable" do |debstable|
    debstable.vm.box = "debian/bullseye64"
    debstable.vm.provision "shell", inline: <<-SHELL
#{apt} update
#{apt} full-upgrade
#{apt} install xauth
#{apt} install python3-yaml python3-schema python3-pyside2.qtcore python3-pyside2.qtgui python3-pyside2.qtwidgets python3-gst-1.0 gir1.2-gtk-3.0
#{apt} install gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly
#{apt} install pulseaudio pulseaudio-utils gstreamer1.0-pulseaudio alsa-utils
adduser vagrant audio
amixer set Master 100%
amixer set Master unmute
amixer set PCM 100%
amixer set PCM unmute
    SHELL
  end

  config.vm.define "debtesting" do |debtesting|
    debtesting.vm.box = "debian/testing64"
    debtesting.vm.provision "shell", inline: <<-SHELL
#{apt} update
#{apt} full-upgrade
#{apt} install xauth
#{apt} install python3-yaml python3-schema python3-pyside2.qtcore python3-pyside2.qtgui python3-pyside2.qtwidgets python3-gst-1.0 gir1.2-gtk-3.0
#{apt} install gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly
#{apt} install pipewire gstreamer1.0-pipewire wireplumber alsa-utils
adduser vagrant audio
amixer set Master 100%
amixer set Master unmute
amixer set PCM 100%
amixer set PCM unmute
    SHELL
  end

end
