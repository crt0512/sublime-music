Settings
########

There are many settings available in Sublime Music. Some are application-wide
settings while others are are configurable on a per-music-provider basis.

Application Settings
--------------------

The following settings can be changed for the entire application. They can be
accessed via the gear icon in the header.

Enable Song Notifications : (``bool``)
    If toggled on, a notification will be shown whenever a new song starts to
    play. The notification will contain the title, artist name, album name, and
    cover art of the song.

Replay Gain : (Disabled | Track | Album)
    Configures the replay gain setting for the MPV player. You can disable this
    setting, or configure it to work on a track or album basis.

Serve Local Files to Chromecasts on the LAN : (``bool``)
    If checked, a local server will be started on your computer which will serve
    your locally cached music files to the Chromecast. If not checked, the
    Chromecast will always stream from the server.
    If you are having trouble playing on your Chromecast, try disabling this
    feature.

Enable Chromecast / Enable UPnP / DLNA : (``bool``)
    Whether to look for Chromecasts, resp. UPnP / DLNA renderers (network
    speakers, AV receivers, TVs, Kodi...), on the LAN. Switching one off also
    stops its LAN server and hides its settings. The device button in the
    player bar is hidden when both are off.

Stream Songs Through This Computer : (``bool``, default on)
    If on, UPnP / DLNA renderers get every song from Sublime Music: locally
    cached songs directly, and the others relayed from the music server. The
    renderer then only ever talks plain HTTP to a machine on its own network,
    which spares it HTTPS, VPNs and whatever else it may not cope with (many
    renderers are picky about what they will fetch). If off, the renderer
    streams everything from the music server directly.

    Songs in a format the renderer can't play (it says which it plays, and is
    believed when it refuses or immediately stops one) are transcoded to MP3:
    the music server is asked to, and if it doesn't, Sublime Music does it with
    ``ffmpeg`` (when it is installed), which takes a few seconds before the
    song starts.

The LAN servers listen on a free port picked by the operating system each time
Sublime Music starts; there is nothing to configure. If a firewall sits between
Sublime Music and the devices, it has to let the devices reach this computer.

Allow Song Downloads : (``bool``)
    If toggled on, this will allow songs to be downloaded and cached locally.

When Streaming, Also Download Song : (``bool``)
    If toggled on, when a song is streamed, it will also be downloaded. Once the
    download is complete, Sublime Music will stop streaming and switch to the
    downloaded version (if playing locally with MPV).

Number of Songs to Prefetch : (``int``)
    If the next :math:`n` songs in the play queue are not already downloaded,
    they will be downloaded.

Maximum Concurrent Downloads : (``int``)
    Specifies the maximum number of songs that should be downloaded at the same
    time.

Music Provider Settings
-----------------------

Each server has the following configuration options:

Music Provider Name : (``string``)
    The friendly name of this server which is used throughout the UI to
    differentiate music providers.

Subsonic Music Provider Settings
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The following settings are configurable when using Subsonic as the music
provider.

Server Address : (URI)
    The URI of the Subsonic-API-compliant server (specify the server root, do
    not include ``/api`` or any other suffix). If you don't include ``https://``
    at the front o

Username : (``string``)
    The username to login as.

Password : (``string``)
    The password to login with. Sublime Music will attempt to store it in the
    system keyring, but fall back to storing it in plaintext in the
    configuration.

Verify Certificate : (``bool``, default ``True``)
    Whether or not to verify the SSL certificate of the server.

    .. danger::

       Only turn this off if you are *absolutely certain* that your connection
       to your server is secure. One reasons this may be necessary is if you
       have a self-signed certificate.

Sync Play Queue : (``bool``, default ``True``)
    If on, Sublime Music will periodically save the play queue state so that you
    can resume on other devices.

    .. admonition:: Details

       Sublime Music will synchronise the play queue and song progress to the
       server every 15 seconds while playing a song. It will also sync whenever
       a song is is paused/played, or when the play queue is edited, and
       whenever a new song is started.

Local Network SSID : (``string``)
    If Sublime Music is connected to the given SSID, the Local Network Address
    will be used instead of the Server address when making network requests.

    .. admonition:: Details

       If this SSID is active (as reported by NetworkManager), then the *Local
       network address* will be used to connect to the Subsonic-API-compliant
       server instead of the *Server address*.

       This is useful if your internet provider does not allow you to connect to
       your internal network using the external IP of your network.

Local Network Address : (``string``)
    See *Local network SSID*
