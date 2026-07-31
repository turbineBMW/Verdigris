pragma Singleton
import QtQuick

// Tahoe-ish palette. Kept in one place so the pages stay consistent and
// the light/dark split is a single switch.
QtObject {
    property bool dark: true

    // Bubbles are translucent so KWin's blur behind the window frosts
    // through them, rather than sitting on them as flat colour.
    // Outgoing alpha is the frost knob: at 0.82 the gradient was effectively
    // opaque and KWin's blur never showed through. ~0.5 lets it read as glass
    // while keeping white body text legible against a light backdrop.
    readonly property color bubbleOutTop:  Qt.rgba(0.21, 0.64, 1.00, 0.52)
    readonly property color bubbleOutBot:  Qt.rgba(0.02, 0.46, 0.94, 0.52)
    readonly property color bubbleIn:      dark ? Qt.rgba(0.42, 0.42, 0.46, 0.34)
                                               : Qt.rgba(1, 1, 1, 0.55)
    readonly property color bubbleInText:  dark ? "#ffffff" : "#000000"
    // Faint top-edge highlight — what sells glass over plain transparency.
    readonly property color bubbleRim:     dark ? Qt.rgba(1, 1, 1, 0.14)
                                               : Qt.rgba(1, 1, 1, 0.55)

    // Alphas are deliberately low: KWin blurs whatever is behind the
    // window, and these tints sit on top of that frosted result.
    readonly property color sidebarBg:     dark ? Qt.rgba(0.16, 0.16, 0.18, 0.42)
                                               : Qt.rgba(1, 1, 1, 0.45)
    readonly property color chromeBg:      dark ? Qt.rgba(0.11, 0.11, 0.12, 0.35)
                                               : Qt.rgba(1, 1, 1, 0.40)
    readonly property color pageBg:        dark ? Qt.rgba(0.07, 0.07, 0.08, 0.30)
                                               : Qt.rgba(0.97, 0.97, 0.98, 0.35)
    // Same hue at full alpha — gradients must fade from an opaque stop,
    // otherwise the "solid" end is already see-through.
    readonly property color pageBgSolid:   dark ? Qt.rgba(0.08, 0.08, 0.09, 1.0)
                                               : Qt.rgba(0.97, 0.97, 0.98, 1.0)

    readonly property color text:          dark ? "#f2f2f7" : "#1c1c1e"
    readonly property color textDim:       dark ? "#98989f" : "#6c6c70"
    readonly property color separator:     dark ? Qt.rgba(1, 1, 1, 0.09)
                                               : Qt.rgba(0, 0, 0, 0.09)
    readonly property color accent:        "#2f8fff"
    readonly property color selection:     "#0a72e8"

    // Icons live in qtui/assets/ (sibling of qml/).
    // Theme.qml is at qml/iphonebridge/, so two levels up lands on qtui/.
    function assetUrl(name) {
        return Qt.resolvedUrl("../../assets/" + name)
    }

    // Lucide icons (ISC) under assets/icons/<name>.svg — use via Icon {}.
    function iconUrl(name) {
        return Qt.resolvedUrl("../../assets/icons/" + name + ".svg")
    }

    // Format → Bigger / Smaller. Multiplies message body text only, the way
    // Messages.app's text-size control does — chrome and timestamps keep
    // their size so the layout doesn't drift.
    property real fontScale: 1.0

    readonly property int radiusBubble: 18
    readonly property int radiusRow: 10
    readonly property int animFast: 120
    readonly property int animBase: 180
    // A message arriving, and the receipt caption that follows it. Slower
    // than animBase on purpose: this one is meant to be watched, where the
    // rest of the motion here is meant to get out of the way.
    readonly property int animArrive: 300
}
