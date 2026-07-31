import QtQuick
import QtQuick.Effects
import QtQuick.Window
import iphonebridge

// Colourable Lucide (or other monochrome) SVG. Source glyphs use white
// strokes so MultiEffect can tint them to `color` without baking a hue
// into the asset.
Item {
    id: root

    property string name: ""
    property color color: Theme.text
    property int size: 16

    // Optional override — defaults to assets/icons/<name>.svg
    property url source: name === "" ? "" : Theme.iconUrl(name)

    width: size
    height: size
    implicitWidth: size
    implicitHeight: size

    Image {
        id: img
        anchors.fill: parent
        source: root.source
        sourceSize.width: root.size * Math.ceil(Screen.devicePixelRatio * 2)
        sourceSize.height: sourceSize.width
        smooth: true
        mipmap: true
        visible: false
        fillMode: Image.PreserveAspectFit
    }

    MultiEffect {
        anchors.fill: img
        source: img
        colorization: 1.0
        colorizationColor: root.color
        // Avoid flashing a full-white glyph before the tint applies.
        opacity: img.status === Image.Ready ? 1 : 0
        Behavior on colorizationColor {
            ColorAnimation { duration: Theme.animFast }
        }
        Behavior on opacity {
            NumberAnimation { duration: Theme.animFast }
        }
    }
}
