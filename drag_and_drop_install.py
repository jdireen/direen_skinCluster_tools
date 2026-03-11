"""Drag-and-drop installer for direen_skinCluster_tools.

Drag this file into the Maya viewport to install the module.
It copies the module files into Maya's base modules directory so the
tool persists independently of the downloaded project folder.
"""

from __future__ import print_function

import os
import shutil

from maya import cmds


_MODULE_NAME = "direen_skinCluster_tools"

_PRESS_CMD = (
    "import direen_skinCluster_tools\n"
    "mm = direen_skinCluster_tools.SkinningMarkingMenu()\n"
)
_RELEASE_CMD = "mm.remove()\n"


def _install_module():
    """Copy module files into Maya's base modules directory.

    Layout created under <MAYA_APP_DIR>/modules/:
        direen_skinCluster_tools.mod
        direen_skinCluster_tools/
            scripts/
                direen_skinCluster_tools.py

    Returns (mod_dest, install_dir) or None on failure.
    """

    project_root = os.path.dirname(os.path.abspath(__file__))
    scripts_dir = os.path.join(project_root, "scripts")
    script_file = os.path.join(scripts_dir, "{}.py".format(_MODULE_NAME))

    if not os.path.isfile(script_file):
        cmds.warning(
            "Cannot find scripts/{}.py next to this installer script.".format(
                _MODULE_NAME
            )
        )
        return None

    maya_app_dir = os.environ.get(
        "MAYA_APP_DIR", os.path.join(os.path.expanduser("~"), "maya")
    )
    modules_dir = os.path.join(maya_app_dir, "modules")
    install_dir = os.path.join(modules_dir, _MODULE_NAME)
    install_scripts_dir = os.path.join(install_dir, "scripts")

    if not os.path.isdir(install_scripts_dir):
        os.makedirs(install_scripts_dir)

    # Copy the script and license into the install location.
    shutil.copy2(script_file, install_scripts_dir)
    license_file = os.path.join(project_root, "LICENSE")
    if os.path.isfile(license_file):
        shutil.copy2(license_file, install_scripts_dir)

    # Write the .mod file pointing to the sibling folder.
    mod_dest = os.path.join(modules_dir, "{}.mod".format(_MODULE_NAME))
    mod_content = "+ {} 1.0 ./{}\n".format(_MODULE_NAME, _MODULE_NAME)

    with open(mod_dest, "w") as f:
        f.write(mod_content)

    print("[{}] Installed module to: {}".format(_MODULE_NAME, install_dir))
    print("[{}] Mod file: {}".format(_MODULE_NAME, mod_dest))
    return mod_dest, install_dir


def _get_existing_hotkey_info(key, ctrl=False, alt=False):
    """Return a description of any existing hotkey binding, or None."""
    try:
        name = cmds.hotkey(key, query=True, ctl=ctrl, alt=alt, name=True) or ""
    except RuntimeError:
        return None

    if not name or name == "":
        return None

    # Resolve the named command to its annotation for a readable description.
    annotation = ""
    if cmds.runTimeCommand(name, exists=True):
        annotation = cmds.runTimeCommand(name, query=True, annotation=True) or ""

    if annotation:
        return "{} ({})".format(name, annotation)
    return name


def _prompt_hotkey_binding():
    """Prompt the user to optionally bind SkinningMarkingMenu to a hotkey.

    Returns True if a hotkey was bound, False otherwise.
    """
    result = cmds.confirmDialog(
        title="Bind Hotkey",
        message=(
            "Would you like to bind the SkinningMarkingMenu to a hotkey?\n\n"
            "You will be prompted to choose a key combination."
        ),
        button=["Yes", "No"],
        defaultButton="Yes",
        cancelButton="No",
        dismissString="No",
    )
    if result != "Yes":
        return False

    # Prompt for the key.
    prompt_result = cmds.promptDialog(
        title="Hotkey",
        message="Enter a key (suggested: m):",
        button=["OK", "Cancel"],
        defaultButton="OK",
        cancelButton="Cancel",
        dismissString="Cancel",
    )
    if prompt_result != "OK":
        return False

    key = cmds.promptDialog(query=True, text=True).strip()
    if not key:
        cmds.warning("No key entered. Skipping hotkey binding.")
        return False

    # Prompt for modifiers.
    mod_result = cmds.confirmDialog(
        title="Modifiers",
        message="Choose modifier keys for '{}' (suggested: None)".format(key),
        button=["Ctrl", "Alt", "Ctrl+Alt", "None", "Cancel"],
        defaultButton="None",
        cancelButton="Cancel",
        dismissString="Cancel",
    )
    if mod_result == "Cancel":
        return False

    use_ctrl = mod_result in ("Ctrl", "Ctrl+Alt")
    use_alt = mod_result in ("Alt", "Ctrl+Alt")

    # Build a readable description of the chosen combo.
    combo_parts = []
    if use_ctrl:
        combo_parts.append("Ctrl")
    if use_alt:
        combo_parts.append("Alt")
    combo_parts.append(key)
    combo_label = "+".join(combo_parts)

    # Check for existing binding.
    existing = _get_existing_hotkey_info(key, ctrl=use_ctrl, alt=use_alt)
    if existing:
        overwrite = cmds.confirmDialog(
            title="Hotkey Conflict",
            message=(
                "The hotkey '{}' is already assigned to:\n\n"
                "    {}\n\n"
                "Overwrite?"
            ).format(combo_label, existing),
            button=["Overwrite", "Cancel"],
            defaultButton="Cancel",
            cancelButton="Cancel",
            dismissString="Cancel",
        )
        if overwrite != "Overwrite":
            cmds.warning("Hotkey binding cancelled.")
            return False

    # Create the runtime commands and assign the hotkey.
    press_cmd_name = "direenSkinMMPress"
    release_cmd_name = "direenSkinMMRelease"

    for cmd_name in (press_cmd_name, release_cmd_name):
        if cmds.runTimeCommand(cmd_name, exists=True):
            cmds.runTimeCommand(cmd_name, edit=True, delete=True)

    cmds.runTimeCommand(
        press_cmd_name,
        annotation="direen SkinningMarkingMenu Press",
        category="User",
        commandLanguage="python",
        command=_PRESS_CMD,
    )
    cmds.runTimeCommand(
        release_cmd_name,
        annotation="direen SkinningMarkingMenu Release",
        category="User",
        commandLanguage="python",
        command=_RELEASE_CMD,
    )

    press_name_cmd = cmds.nameCommand(
        "direenSkinMMPressNameCmd",
        annotation="direen SkinningMarkingMenu Press",
        sourceType="mel",
        command=press_cmd_name,
    )
    release_name_cmd = cmds.nameCommand(
        "direenSkinMMReleaseNameCmd",
        annotation="direen SkinningMarkingMenu Release",
        sourceType="mel",
        command=release_cmd_name,
    )

    cmds.hotkey(
        keyShortcut=key,
        ctl=use_ctrl,
        alt=use_alt,
        name=press_name_cmd,
        releaseName=release_name_cmd,
    )

    cmds.savePrefs(hotkeys=True)

    print("[{}] Hotkey bound: {} (press/release)".format(_MODULE_NAME, combo_label))
    return True


def onMayaDroppedPythonFile(*args, **kwargs):
    """Entry point called by Maya when a .py file is dropped into the viewport."""

    result = _install_module()
    if result is None:
        return

    mod_dest, install_dir = result

    hotkey_bound = _prompt_hotkey_binding()

    # Final summary.
    hotkey_msg = ""
    if hotkey_bound:
        hotkey_msg = "\nHotkey preference saved.\n"

    cmds.confirmDialog(
        title="{} Installed".format(_MODULE_NAME),
        message=(
            "Module installed successfully.\n\n"
            "Mod file: {mod}\n"
            "Installed to: {dest}\n"
            "{hotkey}\n"
            "Please restart Maya for the module to take effect."
        ).format(mod=mod_dest, dest=install_dir, hotkey=hotkey_msg),
        button=["OK"],
    )

    print("[{}] Restart Maya to activate.".format(_MODULE_NAME))
