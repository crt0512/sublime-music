import shutil
from pathlib import Path

import pytest

from sublime_music.adapters import ConfigurationStore
from sublime_music.adapters.filesystem import FilesystemAdapter
from sublime_music.adapters.subsonic import SubsonicAdapter
from sublime_music.config import AppConfiguration, ProviderConfiguration


@pytest.fixture
def config_filename(tmp_path: Path):
    yield tmp_path.joinpath("config.json")


@pytest.fixture
def cwd():
    yield Path(__file__).parent


def test_config_default_cache_location():
    config = AppConfiguration()
    assert config.cache_location == Path("~/.local/share/sublime-music").expanduser()


def test_server_property(tmp_path: Path):
    config = AppConfiguration()
    config.cache_location = tmp_path
    provider = ProviderConfiguration(
        id="1",
        name="foo",
        ground_truth_adapter_type=SubsonicAdapter,
        ground_truth_adapter_config=ConfigurationStore(),
    )
    config.providers["1"] = provider
    assert config.provider is None
    config.current_provider_id = "1"
    assert config.provider == provider

    assert config._state_file_location == tmp_path.joinpath("1", "state.pickle")


def test_json_load_unload(config_filename: Path, tmp_path: Path):
    ConfigurationStore.MOCK = True
    subsonic_config_store = ConfigurationStore(username="test")
    subsonic_config_store.set_secret("password", "testpass")
    original_config = AppConfiguration(
        providers={
            "1": ProviderConfiguration(
                id="1",
                name="foo",
                ground_truth_adapter_type=SubsonicAdapter,
                ground_truth_adapter_config=subsonic_config_store,
                caching_adapter_type=FilesystemAdapter,
                caching_adapter_config=ConfigurationStore(),
            )
        },
        current_provider_id="1",
        filename=config_filename,
    )
    original_config.cache_location = tmp_path

    original_config.save()

    loaded_config = AppConfiguration.load_from_file(config_filename)

    assert original_config.version == loaded_config.version
    assert original_config.providers == loaded_config.providers
    assert original_config.provider == loaded_config.provider
    assert original_config.provider and loaded_config.provider
    assert (
        original_config.provider.ground_truth_adapter_config.items()
        == loaded_config.provider.ground_truth_adapter_config.items()
    )


UPNP_DEFAULTS = {"Stream Songs Through This Computer": True}


def test_config_migrate_v5_to_current(config_filename: Path, cwd: Path):
    shutil.copyfile(str(cwd.joinpath("mock_data/config-v5.json")), str(config_filename))
    app_config = AppConfiguration.load_from_file(config_filename)
    app_config.migrate()

    assert app_config.version == 8
    assert app_config.player_config == {
        "Local Playback": {"Replay Gain": "track"},
        "Chromecast": {"Serve Local Files to Chromecasts on the LAN": True},
        "UPnP / DLNA": UPNP_DEFAULTS,
    }
    app_config.save()
    app_config2 = AppConfiguration.load_from_file(config_filename)
    assert app_config == app_config2


def test_config_migrate_v6_to_v8(tmp_path: Path):
    # v8 adds the UPnP player's settings and drops the LAN server port settings.
    config_filename = tmp_path / "config.json"
    config_filename.write_text(
        '{"version": 6, "providers": {}, "player_config": {"Local Playback": '
        '{"Replay Gain": "Album"}, "Chromecast": {"LAN Server Port Number": 1, '
        '"Serve Local Files to Chromecasts on the LAN": false}}}'
    )

    config = AppConfiguration.load_from_file(config_filename)

    assert config.version == 8
    assert config.upnp_enabled is True
    assert config.player_config == {
        "Local Playback": {"Replay Gain": "Album"},
        "Chromecast": {"Serve Local Files to Chromecasts on the LAN": False},
        "UPnP / DLNA": UPNP_DEFAULTS,
    }


def test_config_migrate_v7_to_v8_renames_the_upnp_setting(tmp_path: Path):
    config_filename = tmp_path / "config.json"
    config_filename.write_text(
        '{"version": 7, "providers": {}, "player_config": {"UPnP / DLNA": '
        '{"Serve Local Files to UPnP Devices on the LAN": false, "LAN Server Port Number": 2}}}'
    )

    config = AppConfiguration.load_from_file(config_filename)

    assert config.version == 8
    # The draft setting meant something else, so the new one gets its default.
    assert config.player_config == {"UPnP / DLNA": UPNP_DEFAULTS}


def test_new_settings_default_for_old_config_files(tmp_path: Path):
    # A config file written before these settings existed must still load, with the
    # defaults filled in.
    config_filename = tmp_path / "config.json"
    config_filename.write_text('{"version": 6, "providers": {}}')

    config = AppConfiguration.load_from_file(config_filename)

    assert config.play_from_here_count == 128
    assert config.confirm_queue_replacement is True
    assert config.queue_replacement_warning_size == 1
    assert config.song_library_sync_interval_minutes == 0
