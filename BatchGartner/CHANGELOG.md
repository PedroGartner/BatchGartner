# Changelog

## 1.5
### Added
- **Create Read in Nuke:** right-click a finished job to create a Read node for the render in the Nuke session that has the script open, next to its Write node.
- **EXR preview** in the built-in preview (needs `OpenEXR` + `numpy`; OpenCV or OpenImageIO also work).
- **Simultaneous renders** (Preferences › Performance). Renders up to 4 jobs at once with shared thread and cache limits. The default is 1.

### Changed
- Right-click menu: common actions at the top level, the rest under *More*.
- Nuke RAM in the system monitor measures the Nuke process (now and peak).
- Locked jobs can't be edited, removed, pasted over or re-rendered.
- Resource protection is less strict (Normal keeps 2 GB free) and has a maximum wait of 10 minutes by default.
- Paste Job Settings skips Write nodes that don't exist in the target script.

## 1.4
### Changed
- Status is shown as a coloured pill for states that need attention (Rendering, Failed, Stopped, Blocked) and a dot for the rest.
- A detail line after the script name (output folder, notes, queued time) tells identical jobs apart.
- The Frames column shows progress while rendering, and the Finish column shows the ETA, or the finish time and render duration.
- The bottom area has four tabs: Tasks, Log, Details and History. Details is marked when the selected job has errors.
- Job Properties shows the preview first, with rarely used settings in a collapsible section.
- Toolbar: START is highlighted, RENDER SELECTED was added, and PAUSE stays lit while a pause is pending.

### Fixed
- The app did not open when the queue contained a finished job.
- Startup errors are shown in a window and written to `batch_gartner_errors.log`.
- The Details and History tabs showed empty panels.

## 1.3
### Added
- **Automatic resume:** failed or stopped jobs render only the missing frames. Half-written frames are removed after stop, skip or timeout.
- **Overwrite protection:** *Render Missing Only / Overwrite All / Cancel* when frames already exist.
- **Built-in frame preview** of finished frames, with a scrub slider. It never locks files.

### Fixed
- A frame that can't be written (locked file) is retried before the job fails.
- Error types are read from the actual error lines.
- The end-of-queue message reports failed jobs.
- The queue table always fills the full width.

## 1.2
### Added
- **Script snapshots:** each job renders the script as it was when queued, so you can keep editing in Nuke. START asks when a script was saved after queuing.

### Fixed
- Helper processes no longer open console windows on Windows.

## 1.1
### Fixed
- Re-rendering a finished job, test-frame status, *Pause after current job*, dependencies on failed jobs.
- STOP, skip and timeout for renders that print no output. Crash-safe render thread. UTF-8 output.
- Jobs are tracked by id, so editing the queue during a render is safe.
- `.nk` parsing of quoted paths, expressions and the Root frame range.
- Crash recovery, atomic settings, single instance, a robust Nuke connection, and the Nuke executable selector.

## 1.0
- Initial release.
