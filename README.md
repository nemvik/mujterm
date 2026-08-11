# MujTerm

MujTerm is a native Linux terminal that keeps project terminals and coding-agent
status visible in one sidebar. It uses GTK 3 and VTE for terminal rendering and
an isolated tmux server so terminal processes survive closing the window.

## Features

- Persistent terminal sessions grouped by project.
- Persistent SSH projects backed by standard OpenSSH configuration and ssh-agent.
- Independent full-workspace terminal pages and a modal keyboard navigation mode.
- Nested horizontal and vertical split panes with draggable dividers.
- Mouse-wheel scrollback in shells and full-screen Codex/Claude interfaces.
- Exact OSC 133 command blocks with exit status, live output, Ghost Diff, and Impact Lens.
- A quiet per-terminal radar for work, attention, completion, errors, resource pressure, and services.
- Live current-directory and Git branch labels.
- Live per-session CPU and resident-memory totals for the complete process tree.
- Attention queue for agents waiting on input, with one-key navigation.
- Local service radar with clickable listening ports and process controls.
- Live dependency map inferred from TCP connections between terminal process trees.
- Persistent project timeline for agent, Git, service, terminal, and layout events.
- Privacy-conscious agent handoff cards with Git, service, resource, and activity context.
- Codex ↔ Claude solution races on isolated Git branches and worktrees, with a comparison dashboard.
- One-click terminal switching, duplication from the live working directory, and closing.
- Drag-and-drop project and terminal organization.
- A global command toolbox for saving and inserting named one-line commands.
- Exact Codex and Claude Code states: working, needs input, ready, and error.
- Safe, opt-in lifecycle hooks that never approve actions or record prompts.
- Automatic clean-shell recovery after a machine reboot.

## Run from the repository

Ubuntu 22.04 already provides the runtime packages used by this project. On a
fresh Debian or Ubuntu installation, install them with:

```sh
sudo apt install python3 python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 tmux git openssh-client
```

Then run:

```sh
./bin/mujterm
```

MujTerm stores its model under the normal XDG data directories and uses its own
tmux socket. It does not import, modify, or terminate unrelated tmux sessions.

## Agent status integration

Open `⋯` → **Agent Integrations…** and choose **Enable**. MujTerm merges its
handlers into the existing Claude Code and Codex hook configuration after making
a timestamped backup. The handlers only become active for processes inheriting a
`MUJTERM_TERMINAL_ID`, so agents launched in other terminals are ignored.

Codex requires one additional safety step: launch Codex in MujTerm, enter
`/hooks`, review the MujTerm command, and trust it. Integrations can be removed
from the same integrations dialog without touching other hooks.

## Keyboard shortcuts

- `Ctrl+Shift+T`: new full-workspace terminal in the active terminal's current directory
- `Ctrl+Shift+Right`: split the active pane to the right
- `Ctrl+Shift+Down`: split the active pane downward
- `Ctrl+Shift+A`: jump to the next agent waiting for input
- `Ctrl+Shift+F`: find text in the active terminal's output
- `Ctrl+Space`: enter or leave keyboard mode
- `Ctrl+Shift+C` / `Ctrl+Shift+V`: copy / paste
- `Ctrl++` / `Ctrl+-`: terminal font zoom
- `Ctrl+Q`: close the window while leaving tmux sessions running

Detected localhost ports appear below their terminal. Click a port to open it in
the default browser; right-click to copy its URL or stop the owning process after
confirmation. The active project's timeline is available from the `⋯` menu.

The agent handoff action creates a reviewable context card and can launch either
agent in a new terminal. Handoffs, the timeline, service map, SSH projects, and
agent integration controls are available from the `⋯` menu. The agent race action
gives Codex and Claude the same task in separate
Git worktrees, opens them side by side, and keeps their diff/resource comparison
available afterward. Worktrees are stored below MujTerm's XDG data directory;
MujTerm does not automatically delete race branches or worktrees containing work.
The network button shows dependencies inferred from live TCP connections. It
does not inspect payloads or terminal output.

Drag with the left mouse button to select terminal text; releasing the button
copies it to the desktop clipboard. Keep dragging at the top or bottom edge to
scroll through tmux history and extend the selection beyond the visible screen.
`Ctrl+Shift+C` and the context-menu **Copy** action copy the most recent selection
again. Hold Shift while dragging to use VTE's local selection instead. Right-click
a detected web address to open or copy it.

Press `Ctrl+Shift+F` to open the active terminal's search bar. Enter and
Shift+Enter move between highlighted matches. Select **PROJECT** in that bar to
search the complete tmux history of every session in the active project; choosing
a result focuses that terminal and carries the query back into its search bar.

Select **BLOCKS** in a terminal HUD to open its semantic command history. New
local Bash, Zsh, and Fish sessions emit standard OSC 133 boundaries and send the
exact command, working directory, and exit status over MujTerm's private Unix
socket. Existing sessions and remote SSH shells continue to use bounded
best-effort prompt detection until their shell is restarted. Repeating the same
command adds a Ghost Diff with added and removed lines.

Each expanded block includes an **Impact Lens**. It compares bounded Git status
metadata before and after the command, records branch and commit transitions,
new or closed listening ports, peak CPU, and resident-memory growth. It never
stores file contents. Click a block to expand it, or choose **CLEAR** to forget
the in-memory history. Commands, output, and impact metadata in this panel are
never persisted to disk. MujTerm sources the normal user Bash/Zsh configuration
through a private generated wrapper; it does not edit shell dotfiles.

The small dot and subtle terminal border form the quiet radar: cyan means work
is running, amber needs attention, green has just completed, pink marks an error
or ended terminal, magenta signals high CPU/RAM pressure, and blue indicates a
listening local service. Hover the dot for the current reason.

Select **CMD** in the header to open the command toolbox, then choose
**+ ADD COMMAND**. Add a name and a single-line command, then click the saved
item to insert its text into the active terminal. Toolbox clicks never press
Enter, so the command remains editable at the prompt until you run it yourself.
Saved commands are available across all projects and are stored as plain text in
MujTerm's local state database.

Select **SSH** in the header to create a persistent VPS project. Enter a project
name, an SSH target such as `root@example.com` (or an alias from
`~/.ssh/config`), and an optional port. The first terminal connects immediately,
and every new, duplicated, split, restored, or restarted terminal in that project
uses the same connection. Host-key and password prompts appear normally inside
the terminal; MujTerm never stores passwords or private keys.

Mouse scrolling is handled by tmux. In a regular shell it enters scrollback mode;
scroll back to the bottom or press `q` to return immediately. Applications that
support mouse input, including full-screen agent interfaces, receive the wheel
events directly.

New terminals always open as their own full workspace; only the explicit split
actions divide the current workspace. In keyboard mode use `h/j/k/l` to move
between panes, `[` and `]` to switch workspaces, `n` for a new full workspace,
`v` to split right, `s` to split down, `x` to close, `a` for the attention queue,
and `q` or `Esc` to leave the mode.

## Tests and packaging

```sh
make check
make deb
```

The Debian package is written to `dist/mujterm_0.1.0_all.deb`.
