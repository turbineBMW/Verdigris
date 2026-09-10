import QtQuick

// ListView with eased wheel scrolling.
//
// A bare ListView jumps the content by a fixed step per wheel notch, which
// reads as steppy. This chases a target position instead, and accumulates
// that target across notches so spinning the wheel quickly keeps
// accelerating rather than restarting from wherever the last step landed.
//
// The chase is a per-frame integrator, not a NumberAnimation. An animation
// has to be restarted on every notch, and restarting means a fresh easing
// curve and a fresh full duration — so the view was always most of a
// duration behind the wheel, and every notch re-entered the slow tail of
// the curve. That is what made scrolling feel sticky and late. Integrating
// toward a moving target has no restart: a new notch moves the target and
// the very next frame is already heading there.
//
// Touchpads are deliberately left alone: they already deliver fine-grained
// pixel deltas that Flickable handles natively, and intercepting them
// would make two-finger scrolling feel worse, not better.
ListView {
    id: view

    // Emitted for real user scroll input, so callers can drop any
    // "stay pinned to the bottom" behaviour the moment the user takes over.
    signal userScrolled()

    property int wheelStep: 130
    // Higher converges faster. This is a rate, not a duration — the time to
    // settle stays the same whether one notch or ten are in flight.
    property real stiffness: 19

    // Rubber-band at the ends rather than a hard stop.
    boundsBehavior: Flickable.DragAndOvershootBounds
    flickDeceleration: 1800
    maximumFlickVelocity: 5000
    pixelAligned: true
    // Realize more delegates off-screen. With variable-height bubbles the
    // contentHeight is an estimate that shifts as items are created, which
    // makes a scroll target drift under it.
    cacheBuffer: Math.max(800, height * 2)

    readonly property real minContentY: originY - topMargin
    readonly property real maxContentY: Math.max(
        minContentY, originY + contentHeight + bottomMargin - height)

    property real _target: contentY

    // True while the integrator is driving contentY, so the movement
    // handlers can tell "the user grabbed the list" apart from "we are
    // scrolling it". Without this our own writes look like user movement
    // and the handlers cancel the scroll mid-flight.
    property bool _selfDriven: false
    // Set while rubber-banding past an edge, which is the one time the
    // target is legitimately out of bounds.
    property bool _springing: false

    property int overshootMax: 44
    // Off for views that set boundsBehavior to StopAtBounds: the spring aims
    // the target past the edge, which Flickable then refuses to follow, so
    // the integrator chases a position it can never reach.
    property bool bounceOnWheel: true

    // Whether the wheel drives the integrator or writes contentY directly.
    //
    // The integrator does not move a list whose delegates vary as much as
    // message bubbles: `originY` is re-estimated as delegates are realized,
    // so it shifts by thousands of pixels while scrolling a long thread, and
    // the chase never converges on a target expressed against it — it sets
    // `_target`, starts the ticker, and contentY stays put. A direct write
    // has nothing to converge on and is what actually scrolled the message
    // list before this was unified.
    property bool smoothWheel: true

    FrameAnimation {
        id: ticker
        running: false
        onTriggered: {
            if (!view._springing)
                view._target = Math.max(view.minContentY,
                                        Math.min(view.maxContentY, view._target))
            var d = view._target - view.contentY
            if (Math.abs(d) < 0.5) {
                view.contentY = view._target
                view._selfDriven = false
                stop()
                return
            }
            // Frame-rate independent exponential approach: the fraction of
            // the remaining distance covered depends on elapsed time, so a
            // 144Hz display and a 60Hz one settle at the same rate.
            view.contentY += d * (1 - Math.exp(-view.stiffness * frameTime))
        }
    }

    // Holds the rubber-band at full stretch for a beat before releasing it.
    Timer {
        id: springBack
        interval: 90
        onTriggered: {
            view._springing = false
            view._target = Math.max(view.minContentY,
                                    Math.min(view.maxContentY, view._target))
        }
    }

    function _chase() {
        if (!ticker.running) {
            view._selfDriven = true
            ticker.start()
        }
    }

    function stopScroll() {
        ticker.stop()
        springBack.stop()
        _springing = false
        _selfDriven = false
        _target = contentY
    }

    // allowBounce is only true for real wheel input. Programmatic scrolls
    // (jumping to the newest message on open) would otherwise trigger the
    // rubber-band whenever a thread's content is shorter than the viewport,
    // which made short conversations visibly bounce as they opened.
    function scrollBy(dy, allowBounce) {
        var base = ticker.running ? _target : contentY
        var raw = base + dy
        var clamped = Math.max(minContentY, Math.min(maxContentY, raw))

        if (raw !== clamped && allowBounce === true && bounceOnWheel) {
            var over = Math.max(-overshootMax,
                                Math.min(overshootMax, (raw - clamped) * 0.35))
            _springing = true
            _target = clamped + over
            springBack.restart()
        } else {
            springBack.stop()
            _springing = false
            _target = clamped
        }
        _chase()
    }

    function smoothScrollToEnd() {
        // Nothing to do when the content already fits — calling scrollBy
        // with a delta that can't be satisfied is what made short threads
        // bounce the moment they opened.
        if (maxContentY - contentY > 0.5)
            scrollBy(maxContentY - contentY, false)
    }

    WheelHandler {
        acceptedDevices: PointerDevice.Mouse
        onWheel: (event) => {
            var notches = event.angleDelta.y / 120
            if (notches === 0)
                return
            view.userScrolled()
            if (view.smoothWheel) {
                view.scrollBy(-notches * view.wheelStep, true)
                return
            }
            // Direct, clamped against the same bounds the rest of this file
            // uses — the whole point of routing it through here rather than a
            // second handler at the usage site, which is what previously left
            // two disagreeing definitions of the ends.
            view.stopScroll()
            view.contentY = Math.max(
                view.minContentY,
                Math.min(view.maxContentY,
                         view.contentY - notches * view.wheelStep))
        }
    }

    // A drag or flick should cancel the chase, otherwise the two fight over
    // contentY. Only a real drag/flick counts — our own writes also raise
    // movementStarted, and acting on those cancelled every wheel scroll.
    //
    // Via Connections rather than `onDragStarted:` on the root: a signal
    // handler written at the usage site replaces the one declared here, and
    // one that did exactly that silently disabled this for the message list.
    Connections {
        target: view
        function onDragStarted() { view.userScrolled(); view.stopScroll() }
        function onFlickStarted() { view.userScrolled(); view.stopScroll() }
        // Touchpads never reach the WheelHandler above (it only accepts a
        // mouse) and wheel events on a Flickable produce neither a drag nor
        // a flick — so movementStarted is the only signal that a touchpad
        // scroll happened at all. Guarded on _selfDriven because our own
        // writes to contentY raise it too.
        function onMovementStarted() {
            if (!view._selfDriven) {
                view.userScrolled()
                view.stopScroll()
            }
        }
    }
}
