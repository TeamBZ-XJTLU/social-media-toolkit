from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Log, Select, SelectionList, Static


ModeOption = tuple[str, str]


SUPPORTED_MODES: dict[str, list[ModeOption]] = {
    "weibo": [("Keyword search", "keyword"), ("Author posts", "author"), ("Author info", "author-info")],
    "rednote": [("Keyword search", "keyword"), ("Author posts", "author")],
    "douyin": [("Author posts", "author"), ("Author info", "author-info")],
}
PLATFORM_OPTIONS: list[ModeOption] = [
    ("Weibo", "weibo"),
    ("Rednote / Xiaohongshu", "rednote"),
    ("Douyin", "douyin"),
]
WEIBO_POST_TYPES: list[ModeOption] = [
    ("All", "all"),
    ("Hot", "hot"),
    ("Original", "original"),
    ("Following", "following"),
    ("Verified", "verified"),
    ("Media", "media"),
    ("Viewpoint", "viewpoint"),
]
WEIBO_CONTENT_FILTERS: list[ModeOption] = [
    ("All", "all"),
    ("Picture", "picture"),
    ("Video", "video"),
    ("Music", "music"),
    ("Link", "link"),
]


class SocialMediaToolkitApp(App[None]):
    """Textual command builder and runner for the crawler CLI."""

    CSS = """
    Screen {
        background: $surface;
    }

    #layout {
        height: 1fr;
    }

    #form-pane {
        width: 42;
        min-width: 36;
        padding: 1;
        border-right: solid $primary;
    }

    #output-pane {
        padding: 1;
        width: 1fr;
    }

    .section-title {
        text-style: bold;
        margin-top: 1;
        color: $accent;
    }

    Input, Select, SelectionList {
        margin-bottom: 1;
    }

    SelectionList {
        height: 8;
        border: tall $panel;
    }

    #actions {
        height: auto;
        margin-top: 1;
    }

    #actions Button {
        width: 100%;
        margin-bottom: 1;
    }

    #preview {
        border: round $panel;
        padding: 1;
        height: 5;
        margin-bottom: 1;
    }

    #log {
        height: 1fr;
        border: round $panel;
    }
    """
    BINDINGS = [
        ("ctrl+r", "run_command", "Run"),
        ("ctrl+c", "stop_command", "Stop"),
        ("ctrl+enter", "continue_after_login", "Continue"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        default_db: Path | None = None,
        default_user_data_dir: Path | None = None,
        default_headless: bool = False,
        default_id_only: bool = False,
        default_task_id: str | None = None,
        main_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.default_db = default_db
        self.default_user_data_dir = default_user_data_dir
        self.default_headless = default_headless
        self.default_id_only = default_id_only
        self.default_task_id = default_task_id
        self.main_path = main_path or Path(__file__).with_name("main.py")
        self.process: asyncio.subprocess.Process | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            with VerticalScroll(id="form-pane"):
                yield Label("Task", classes="section-title")
                yield Select(PLATFORM_OPTIONS, value="weibo", allow_blank=False, id="platform")
                yield Select(SUPPORTED_MODES["weibo"], value="keyword", allow_blank=False, id="mode")
                yield Input(placeholder="Keyword, author ID, or sec_user_id", id="condition")

                yield Label("Global", classes="section-title")
                yield Input(value=str(self.default_db or ""), placeholder="Database path override", id="db")
                yield Input(
                    value=str(self.default_user_data_dir or ""),
                    placeholder="Browser profile directory override",
                    id="user-data-dir",
                )
                yield Input(value=self.default_task_id or "", placeholder="Task ID override", id="task-id")
                yield Checkbox("Headless browser", value=self.default_headless, id="headless")
                yield Checkbox("ID only", value=self.default_id_only, id="id-only")
                yield Checkbox("Use local index", id="from-local")
                yield Checkbox("Skip comments", id="no-comments")

                yield Label("Limits", classes="section-title")
                yield Input(placeholder="Max pages", id="max-pages")
                yield Input(placeholder="Max empty pages", id="max-empty-pages")
                yield Input(placeholder="Max comment pages", id="max-comment-pages")
                yield Input(placeholder="Max reply pages", id="max-reply-pages")
                yield Input(placeholder="Rednote max no-height increases", id="max-no-height-increase")

                yield Label("Weibo Keyword Filters", classes="section-title")
                yield SelectionList(*WEIBO_POST_TYPES, id="weibo-post-types")
                yield SelectionList(*WEIBO_CONTENT_FILTERS, id="weibo-content-filters")
                yield Input(placeholder="Time from: YYYY-MM-DD or YYYY-MM-DD-HH", id="time-from")
                yield Input(placeholder="Time to: YYYY-MM-DD or YYYY-MM-DD-HH", id="time-to")

                with Vertical(id="actions"):
                    yield Button("Run", variant="success", id="run")
                    yield Button("Continue", variant="primary", id="continue", disabled=True)
                    yield Button("Stop", variant="error", id="stop", disabled=True)
                    yield Button("Clear", id="clear")

            with Vertical(id="output-pane"):
                yield Static("", id="preview", markup=False)
                yield Log(id="log", highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_mode_options()
        self._refresh_enabled_state()
        self._refresh_preview()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "platform":
            self._refresh_mode_options()
        self._refresh_enabled_state()
        self._refresh_preview()

    def on_input_changed(self, _event: Input.Changed) -> None:
        self._refresh_preview()

    def on_checkbox_changed(self, _event: Checkbox.Changed) -> None:
        self._refresh_enabled_state()
        self._refresh_preview()

    def on_selection_list_selected_changed(self, _event: SelectionList.SelectedChanged) -> None:
        self._refresh_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run":
            self.action_run_command()
        elif event.button.id == "continue":
            self.action_continue_after_login()
        elif event.button.id == "stop":
            self.action_stop_command()
        elif event.button.id == "clear":
            self.query_one("#log", Log).clear()

    def action_run_command(self) -> None:
        self.run_command()

    def action_stop_command(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.query_one("#log", Log).write_line("Stopping running command...")
            self.process.terminate()

    def action_continue_after_login(self) -> None:
        log = self.query_one("#log", Log)
        if self.process is None or self.process.returncode is not None:
            log.write_line("No running command is waiting for input.")
            return
        if self.process.stdin is None:
            log.write_line("Running command has no writable input stream.")
            return

        self.process.stdin.write(b"\n")
        log.write_line("Sent Enter to the running command.")

    @work(exclusive=True)
    async def run_command(self) -> None:
        log = self.query_one("#log", Log)
        run_button = self.query_one("#run", Button)
        continue_button = self.query_one("#continue", Button)
        stop_button = self.query_one("#stop", Button)
        args, errors = self._build_command()
        self._refresh_preview()
        if errors:
            for error in errors:
                log.write_line(f"Error: {error}")
            return

        log.write_line(f"$ {shlex.join(args)}")
        run_button.disabled = True
        continue_button.disabled = False
        stop_button.disabled = False
        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                cwd=Path.cwd(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert self.process.stdout is not None
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                log.write_line(line.decode(errors="replace").rstrip())

            return_code = await self.process.wait()
            log.write_line(f"Command exited with code {return_code}.")
        except FileNotFoundError as exc:
            log.write_line(f"Error: {exc}")
        finally:
            self.process = None
            run_button.disabled = False
            continue_button.disabled = True
            stop_button.disabled = True

    def _refresh_mode_options(self) -> None:
        platform = self._select_value("platform")
        modes = SUPPORTED_MODES.get(platform, SUPPORTED_MODES["weibo"])
        mode_select = self.query_one("#mode", Select)
        current = self._select_value("mode")
        mode_select.set_options(modes)
        if current not in {value for _, value in modes}:
            mode_select.value = modes[0][1]

    def _refresh_enabled_state(self) -> None:
        platform = self._select_value("platform")
        mode = self._select_value("mode")
        is_weibo_keyword = platform == "weibo" and mode == "keyword"
        is_rednote = platform == "rednote"
        is_author = mode == "author"
        is_douyin_author = platform == "douyin" and mode == "author"
        is_weibo_author = platform == "weibo" and mode == "author"

        self.query_one("#from-local", Checkbox).disabled = not (
            is_author or (platform == "rednote" and mode == "keyword")
        )
        self.query_one("#no-comments", Checkbox).disabled = not (is_weibo_author or is_douyin_author)
        self.query_one("#max-pages", Input).disabled = not (is_weibo_author or is_weibo_keyword)
        self.query_one("#max-empty-pages", Input).disabled = not (is_weibo_author or is_douyin_author)
        self.query_one("#max-comment-pages", Input).disabled = not (is_weibo_author or is_douyin_author)
        self.query_one("#max-reply-pages", Input).disabled = not is_douyin_author
        self.query_one("#max-no-height-increase", Input).disabled = not is_rednote
        for widget_id in ("#weibo-post-types", "#weibo-content-filters", "#time-from", "#time-to"):
            self.query_one(widget_id).disabled = not is_weibo_keyword

    def _refresh_preview(self) -> None:
        args, errors = self._build_command()
        preview = self.query_one("#preview", Static)
        if errors:
            preview.update("Cannot run:\n" + "\n".join(errors))
            return
        preview.update("$ " + shlex.join(args))

    def _build_command(self) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        platform = self._select_value("platform")
        mode = self._select_value("mode")
        condition = self._input_value("condition")
        if mode not in {value for _, value in SUPPORTED_MODES.get(platform, [])}:
            errors.append(f"{platform} does not support {mode}.")
        if not condition:
            errors.append("Enter a keyword, author ID, or sec_user_id.")

        args = [sys.executable, str(self.main_path)]
        self._add_global_options(args)
        args.extend([platform, mode])

        if platform == "rednote" and mode in {"author", "keyword"}:
            self._add_int_option(args, "max-no-height-increase", "--max-no-height-increase", errors)
            self._add_flag(args, "from-local", "--from-local")
            if condition:
                args.append(condition)
        elif platform == "weibo" and mode == "author":
            self._add_flag(args, "no-comments", "--no-comments")
            self._add_int_option(args, "max-pages", "--max-pages", errors)
            self._add_int_option(args, "max-comment-pages", "--max-comment-pages", errors)
            self._add_int_option(args, "max-empty-pages", "--max-empty-pages", errors)
            self._add_flag(args, "from-local", "--from-local")
            if condition:
                args.append(condition)
        elif platform == "weibo" and mode == "keyword":
            self._add_int_option(args, "max-pages", "--max-pages", errors)
            for value in self._selected_values("weibo-post-types"):
                args.extend(["--post-type", value])
            for value in self._selected_values("weibo-content-filters"):
                args.extend(["--content-filter", value])
            self._add_text_option(args, "time-from", "--time-from")
            self._add_text_option(args, "time-to", "--time-to")
            if condition:
                args.append(condition)
        elif platform == "weibo" and mode == "author-info":
            args.extend(self._split_condition(condition))
        elif platform == "douyin" and mode == "author":
            self._add_flag(args, "no-comments", "--no-comments")
            self._add_int_option(args, "max-pages", "--max-video-pages", errors)
            self._add_int_option(args, "max-comment-pages", "--max-comment-pages", errors)
            self._add_int_option(args, "max-reply-pages", "--max-reply-pages", errors)
            self._add_int_option(args, "max-empty-pages", "--max-empty-pages", errors)
            self._add_flag(args, "from-local", "--from-local")
            if condition:
                args.append(condition)
        elif platform == "douyin" and mode == "author-info":
            if condition:
                args.append(condition)
        elif condition:
            args.append(condition)

        return args, errors

    def _add_global_options(self, args: list[str]) -> None:
        self._add_text_option(args, "db", "--db")
        self._add_text_option(args, "user-data-dir", "--user-data-dir")
        self._add_text_option(args, "task-id", "--task-id")
        self._add_flag(args, "headless", "--headless")
        self._add_flag(args, "id-only", "--id-only")

    def _add_text_option(self, args: list[str], widget_id: str, option: str) -> None:
        value = self._input_value(widget_id)
        if value:
            args.extend([option, value])

    def _add_int_option(self, args: list[str], widget_id: str, option: str, errors: list[str]) -> None:
        value = self._input_value(widget_id)
        if not value:
            return
        try:
            parsed = int(value)
        except ValueError:
            errors.append(f"{option} must be an integer.")
            return
        if parsed < 1:
            errors.append(f"{option} must be at least 1.")
            return
        args.extend([option, str(parsed)])

    def _add_flag(self, args: list[str], widget_id: str, option: str) -> None:
        if self.query_one(f"#{widget_id}", Checkbox).value:
            args.append(option)

    def _select_value(self, widget_id: str) -> str:
        value = self.query_one(f"#{widget_id}", Select).value
        return value if isinstance(value, str) else ""

    def _input_value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def _selected_values(self, widget_id: str) -> list[str]:
        return [str(value) for value in self.query_one(f"#{widget_id}", SelectionList).selected]

    @staticmethod
    def _split_condition(condition: str) -> list[str]:
        return [value for value in condition.replace(",", " ").split() if value]


def run_ui(
    *,
    default_db: Path | None = None,
    default_user_data_dir: Path | None = None,
    default_headless: bool = False,
    default_id_only: bool = False,
    default_task_id: str | None = None,
) -> None:
    SocialMediaToolkitApp(
        default_db=default_db,
        default_user_data_dir=default_user_data_dir,
        default_headless=default_headless,
        default_id_only=default_id_only,
        default_task_id=default_task_id,
    ).run()
