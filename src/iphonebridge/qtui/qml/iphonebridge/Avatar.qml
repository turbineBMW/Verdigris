import QtQuick
import QtQuick.Window

// Circular contact photo with an initials fallback.
//
// The photo arrives already cut into a circle by iphonebridge.avatars, so
// this draws a plain Image. Masking here instead cost two multisampled
// render targets per avatar; with a list of forty conversations that was
// over a hundred framebuffers and it visibly stalled scrolling.
Item {
    id: root
    property string source: ""
    property string initials: "?"
    property int size: 36

    implicitWidth: size
    implicitHeight: size

    Rectangle {
        anchors.fill: parent
        radius: width / 2
        antialiasing: true
        visible: img.status !== Image.Ready
        // Stable per-contact tint, so the same person keeps the same colour.
        color: Qt.hsla((root.initials.charCodeAt(0) % 12) / 12, 0.45, 0.45, 1)

        Text {
            anchors.centerIn: parent
            text: root.initials
            color: "white"
            font.pixelSize: root.size * 0.38
            font.weight: Font.DemiBold
        }
    }

    Image {
        id: img
        anchors.fill: parent
        source: root.source
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        cache: true
        // Decode near display size; the cached PNG is 128px square.
        sourceSize.width: Math.min(
            128, root.size * Math.ceil(Screen.devicePixelRatio * 2))
        sourceSize.height: sourceSize.width
        smooth: true
        mipmap: true
    }
}
