import logging
import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Type, Union

import dataclasses_json
from dataclasses_json import DataClassJsonMixin, config

from .adapters import ConfigurationStore
from .ui.state import UIState


# JSON decoder and encoder translations
def encode_path(path: Path) -> str:
    return str(path.resolve())


dataclasses_json.cfg.global_config.decoders[Path] = Path
dataclasses_json.cfg.global_config.decoders[Optional[Path]] = (  # type: ignore
    lambda p: Path(p) if p else None
)


dataclasses_json.cfg.global_config.encoders[Path] = encode_path
dataclasses_json.cfg.global_config.encoders[Optional[Path]] = encode_path  # type: ignore


@dataclass
class ProviderConfiguration:
    id: str
    name: str
    ground_truth_adapter_type: Type
    ground_truth_adapter_config: ConfigurationStore
    caching_adapter_type: Optional[Type] = None
    caching_adapter_config: Optional[ConfigurationStore] = None

    def migrate(self):
        self.ground_truth_adapter_type.migrate_configuration(self.ground_truth_adapter_config)
        if self.caching_adapter_type:
            self.caching_adapter_type.migrate_configuration(self.caching_adapter_config)

    def clone(self) -> "ProviderConfiguration":
        return ProviderConfiguration(
            self.id,
            self.name,
            self.ground_truth_adapter_type,
            self.ground_truth_adapter_config.clone(),
            self.caching_adapter_type,
            (self.caching_adapter_config.clone() if self.caching_adapter_config else None),
        )

    def persist_secrets(self):
        self.ground_truth_adapter_config.persist_secrets()
        if self.caching_adapter_config:
            self.caching_adapter_config.persist_secrets()

    def asdict(self) -> Dict[str, Any]:
        def get_typename(key: str) -> Optional[str]:
            key += "_type"
            if isinstance(self, dict):
                return type_.__name__ if (type_ := self.get(key)) else None
            else:
                return type_.__name__ if (type_ := getattr(self, key)) else None

        return {
            "id": self.id,
            "name": self.name,
            "ground_truth_adapter_type": get_typename("ground_truth_adapter"),
            "ground_truth_adapter_config": dict(self.ground_truth_adapter_config),
            "caching_adapter_type": get_typename("caching_adapter"),
            "caching_adapter_config": dict(self.caching_adapter_config or {}),
        }


def encode_providers(
    providers_dict: Dict[str, Union[ProviderConfiguration, Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    return {
        id_: (
            config
            if isinstance(config, ProviderConfiguration)
            else ProviderConfiguration(**config)
        ).asdict()
        for id_, config in providers_dict.items()
    }


def decode_providers(
    providers_dict: Dict[str, Dict[str, Any]],
) -> Dict[str, ProviderConfiguration]:
    from sublime_music.adapters import AdapterManager

    def find_adapter_type(type_name: str) -> Type:
        for available_adapter in AdapterManager.available_adapters:
            if available_adapter.__name__ == type_name:
                return available_adapter
        raise Exception(f"Couldn't find adapter of type {type_name}")

    return {
        id_: ProviderConfiguration(
            config["id"],
            config["name"],
            ground_truth_adapter_type=find_adapter_type(config["ground_truth_adapter_type"]),
            ground_truth_adapter_config=ConfigurationStore(
                **config["ground_truth_adapter_config"]
            ),
            caching_adapter_type=(
                find_adapter_type(cat) if (cat := config.get("caching_adapter_type")) else None
            ),
            caching_adapter_config=(
                ConfigurationStore(**(config.get("caching_adapter_config") or {}))
            ),
        )
        for id_, config in providers_dict.items()
    }


def _write_atomically(path: Path, data: bytes):
    """
    Replaces the file at ``path`` with ``data`` all at once: the app being killed or
    crashing while saving leaves the old file, not half of the new one (a config that
    doesn't load is replaced by the defaults, which forgets every music source).
    """
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


# The most "Play from here" may queue, whatever the setting says.
MAX_PLAY_FROM_HERE_COUNT = 1024


@dataclass
class AppConfiguration(DataClassJsonMixin):
    version: int = 5
    cache_location: Optional[Path] = None
    filename: Optional[Path] = None

    # Providers
    providers: Dict[str, ProviderConfiguration] = field(
        default_factory=dict,
        metadata=config(encoder=encode_providers, decoder=decode_providers),
    )
    current_provider_id: Optional[str] = None
    _loaded_provider_id: Optional[str] = field(default=None, init=False)

    # Players
    player_config: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Global Settings
    song_play_notification: bool = True
    offline_mode: bool = False
    allow_song_downloads: bool = True
    download_on_stream: bool = True  # also download when streaming a song
    prefetch_amount: int = 3
    concurrent_download_limit: int = 5

    # Play queue settings: ask before a double-click replaces a play queue which is longer
    # than ``queue_replacement_warning_size`` songs.
    confirm_queue_replacement: bool = True
    queue_replacement_warning_size: int = 1
    # How many songs "Play from here" in the Songs tab queues (the chosen one and the
    # ones after it in the table); at most MAX_PLAY_FROM_HERE_COUNT.
    play_from_here_count: int = 128

    # Song library: sync it automatically every so many minutes (0 = only by hand).
    song_library_sync_interval_minutes: int = 0

    # Chromecast support. Off means no device discovery, no LAN file server and no
    # device button at all.
    chromecast_enabled: bool = True

    # "Go to album" keeps the current album sort when it lists every album; from a view
    # that shows only some (starred, by genre...), it switches to this one instead. The
    # name of an AlbumSearchQuery.Type that lists every album.
    go_to_album_fallback_sort: str = "NEWEST"

    # Hide the "resume the play queue?" prompt after this many seconds (0 = never).
    resume_prompt_timeout_seconds: int = 5

    # The main window's size (its last one when not maximized), and whether it was
    # maximized, when the app last ran.
    window_width: int = 1342
    window_height: int = 756
    window_maximized: bool = False

    # Deprecated. These have also been renamed to avoid using them elsewhere in the app.
    _sol: bool = field(default=True, metadata=config(field_name="serve_over_lan"))
    _pn: int = field(default=8282, metadata=config(field_name="port_number"))
    _rg: int = field(default=0, metadata=config(field_name="replay_gain"))

    @staticmethod
    def load_from_file(filename: Path) -> "AppConfiguration":
        config = AppConfiguration()
        try:
            if filename.exists():
                with open(filename, "r") as f:
                    config = AppConfiguration.from_json(f.read())
        except Exception:
            pass

        config.filename = filename
        return config

    def __post_init__(self):
        # Default the cache_location to ~/.local/share/sublime-music
        if not self.cache_location:
            path = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share")
            path = path.expanduser().joinpath("sublime-music").resolve()
            self.cache_location = path

        self._state = None
        self._loaded_provider_id = None
        self.migrate()

    def migrate(self):
        for _, provider in self.providers.items():
            provider.migrate()

        if self.version < 6:
            self.player_config = {
                "Local Playback": {"Replay Gain": ["no", "track", "album"][self._rg]},
                "Chromecast": {
                    "Serve Local Files to Chromecasts on the LAN": self._sol,
                    "LAN Server Port Number": self._pn,
                },
            }

        self.version = 6
        self.state.migrate()

    @property
    def provider(self) -> Optional[ProviderConfiguration]:
        return self.providers.get(self.current_provider_id or "")

    @property
    def state(self) -> UIState:
        if not (provider := self.provider):
            return UIState()

        # If the provider has changed, then retrieve the new provider's state.
        if self._loaded_provider_id != provider.id:
            self.load_state()

        assert self._state
        return self._state

    def load_state(self):
        self._state = UIState()
        if not (provider := self.provider):
            return

        self._loaded_provider_id = provider.id
        if (state_filename := self._state_file_location) and state_filename.exists():
            try:
                with open(state_filename, "rb") as f:
                    self._state = pickle.load(f)
            except Exception:
                logging.exception(f"Couldn't load state from {state_filename}")
                # Just ignore any errors, it is only UI state.
                self._state = UIState()

        self._state.__init_available_players__()

    @property
    def _state_file_location(self) -> Optional[Path]:
        if not (provider := self.provider):
            return None

        assert self.cache_location
        return self.cache_location.joinpath(provider.id, "state.pickle")

    def save(self):
        assert self.filename
        # Save the config as YAML.
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(self.filename, self.to_json(indent=2, sort_keys=True).encode())

        # Save the state for the current provider.
        if state_filename := self._state_file_location:
            state_filename.parent.mkdir(parents=True, exist_ok=True)
            _write_atomically(state_filename, pickle.dumps(self.state))
