import QtQuick
import QtQuick.Controls
import verdigris

// The window's only navigation row: traffic lights on the left, hamburger
// on the right, and the whole strip drags the window.
//
// Everything else (page switching, showing/hiding the conversation list,
// connection status) lives in the global menu rather than cluttering this.
Item {
    id: chrome
    property int pageIndex: 0
    signal pageRequested(int index)

    implicitHeight: 38

    // Drag anywhere on the bar; double-click maximises, as a titlebar would.
    MouseArea {
        anchors.fill: parent
        onPressed: winctl.startDrag()
        onDoubleClicked: winctl.toggleMaximize()
    }

    Row {
        id: lights
        anchors {
            left: parent.left
            leftMargin: 12
            verticalCenter: parent.verticalCenter
        }
        spacing: 8

        Repeater {
            model: [
                { c: "#ff5f57", act: "close" },
                { c: "#febc2e", act: "minimize" },
                { c: "#28c840", act: "maximize" }
            ]
            delegate: Rectangle {
                required property var modelData
                width: 12; height: 12; radius: 6
                color: modelData.c
                antialiasing: true
                opacity: lightHover.hovered ? 1.0 : 0.9
                Behavior on opacity {
                    NumberAnimation { duration: Theme.animFast }
                }
                HoverHandler { id: lightHover }
                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -2
                    onClicked: {
                        if (modelData.act === "close") winctl.close()
                        else if (modelData.act === "minimize") winctl.minimize()
                        else winctl.toggleMaximize()
                    }
                }
            }
        }
    }

    ToolButton {
        id: menuButton
        anchors {
            right: parent.right
            rightMargin: 6
            verticalCenter: parent.verticalCenter
        }
        flat: true
        implicitWidth: 28
        implicitHeight: 28
        // Accessible name for the global-menu-style page picker.
        Accessible.name: "Menu"
        onClicked: pageMenu.popup(menuButton, 0, menuButton.height)
        // Press shrink only — no hover scale (see AGENTS.md).
        scale: down ? 0.94 : 1.0
        Behavior on scale {
            NumberAnimation {
                duration: Theme.animFast
                easing.type: Easing.OutCubic
            }
        }

        contentItem: Item {
            // ToolButton sizes this to the content area; centre the glyph.
            Icon {
                anchors.centerIn: parent
                name: "menu"
                size: 16
                color: menuButton.hovered || menuButton.down
                       ? Theme.text : Theme.textDim
            }
        }

        background: Rectangle {
            radius: 6
            color: menuButton.down ? Qt.rgba(1, 1, 1, 0.12)
                 : menuButton.hovered ? Qt.rgba(1, 1, 1, 0.08)
                 : "transparent"
            Behavior on color {
                ColorAnimation { duration: Theme.animFast }
            }
        }

        Menu {
            id: pageMenu
            enter: Transition {
                ParallelAnimation {
                    NumberAnimation {
                        property: "opacity"; from: 0; to: 1
                        duration: Theme.animFast
                    }
                    NumberAnimation {
                        property: "scale"
                        from: Theme.popScale; to: 1
                        duration: Theme.animFast
                        easing.type: Easing.OutCubic
                    }
                }
            }
            exit: Transition {
                NumberAnimation {
                    property: "opacity"; to: 0
                    duration: Theme.animMicro
                }
            }
            Repeater {
                model: ["Messages", "Notifications", "Calls", "Setup"]
                delegate: MenuItem {
                    required property int index
                    required property string modelData
                    text: modelData
                    checkable: true
                    checked: chrome.pageIndex === index
                    onTriggered: chrome.pageRequested(index)
                }
            }
        }
    }
}
