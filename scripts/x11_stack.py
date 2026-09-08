"""Read X11 stacking order without moving the pointer or opening another display."""

import ctypes as c
from ctypes.util import find_library


class Attributes(c.Structure):
    # XWindowAttributes from Xlib.h; native widths/alignment matter here.
    _fields_ = [
        (name, c.c_int) for name in ("x", "y", "width", "height", "border", "depth")
    ] + [
        ("visual", c.c_void_p),
        ("root", c.c_ulong),
        ("window_class", c.c_int),
        ("bit_gravity", c.c_int),
        ("win_gravity", c.c_int),
        ("backing_store", c.c_int),
        ("backing_planes", c.c_ulong),
        ("backing_pixel", c.c_ulong),
        ("save_under", c.c_int),
        ("colormap", c.c_ulong),
        ("map_installed", c.c_int),
        ("map_state", c.c_int),
        ("all_event_masks", c.c_long),
        ("your_event_mask", c.c_long),
        ("do_not_propagate_mask", c.c_long),
        ("override_redirect", c.c_int),
        ("screen", c.c_void_p),
    ]


def occluders(window):
    lib = c.CDLL(find_library("X11") or "libX11.so.6")
    pointer, xid = c.c_void_p, c.c_ulong
    signatures = {
        "XOpenDisplay": ([c.c_char_p], pointer),
        "XCloseDisplay": ([pointer], c.c_int),
        "XQueryTree": (
            [
                pointer,
                xid,
                c.POINTER(xid),
                c.POINTER(xid),
                c.POINTER(c.POINTER(xid)),
                c.POINTER(c.c_uint),
            ],
            c.c_int,
        ),
        "XGetWindowAttributes": ([pointer, xid, c.POINTER(Attributes)], c.c_int),
        "XFree": ([pointer], c.c_int),
        "XSetErrorHandler": ([pointer], pointer),
        "XSync": ([pointer, c.c_int], c.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result
    display = lib.XOpenDisplay(None)
    if not display:
        raise RuntimeError("Cannot inspect the session's X11 stacking order")
    errors = []

    @c.CFUNCTYPE(c.c_int, pointer, pointer)
    def on_error(display, event):
        errors.append(True)
        return 0

    previous = lib.XSetErrorHandler(c.cast(on_error, pointer))

    def tree(window):
        root, parent, children, count = xid(), xid(), c.POINTER(xid)(), c.c_uint()
        if not lib.XQueryTree(
            display,
            window,
            c.byref(root),
            c.byref(parent),
            c.byref(children),
            c.byref(count),
        ):
            raise RuntimeError("Window changed while reading X11 stacking order")
        try:
            return root.value, parent.value, list(children[: count.value])
        finally:
            if children:
                lib.XFree(children)

    try:
        for _ in range(64):
            root, parent, _ = tree(window)
            if parent == root:
                break
            if not parent:
                raise RuntimeError("Focused window is not a top-level app")
            window = parent
        else:
            raise RuntimeError("X11 window hierarchy is too deep")
        _, _, siblings = tree(root)
        if window not in siblings:
            raise RuntimeError("Focused window left the X11 stacking order")
        result = []
        for sibling in siblings[siblings.index(window) + 1 :]:
            attributes = Attributes()
            if not lib.XGetWindowAttributes(display, sibling, c.byref(attributes)):
                raise RuntimeError("Window changed while reading X11 stacking order")
            if (
                attributes.map_state == 2
                and attributes.width > 0
                and attributes.height > 0
            ):
                result.append(
                    [
                        sibling,
                        attributes.x,
                        attributes.y,
                        attributes.x + attributes.width + 2 * attributes.border,
                        attributes.y + attributes.height + 2 * attributes.border,
                    ]
                )
        lib.XSync(display, 0)
        if errors:
            raise RuntimeError("Window changed while reading X11 stacking order")
        return result
    finally:
        lib.XCloseDisplay(display)
        lib.XSetErrorHandler(previous)
