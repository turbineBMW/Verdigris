import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import QtQuick.Layouts
import QtQuick.Shapes
// Qt5Compat.GraphicalEffects exports a LinearGradient of its own with a
// different API, and being imported later it wins the unqualified name.
import QtQuick.Shapes as Shapes
import QtQuick.Window
import Qt5Compat.GraphicalEffects
import iphonebridge

Item {
    id: page
    property bool sidebarVisible: true
    property int pageIndex: 0
    signal pageRequested(int index)

    // Clicking away from something drops it. Without this a message stayed
    // highlighted and the composer kept its caret no matter where you clicked
    // next, so the window always looked like it was mid-edit.
    //
    // Driven off `Window.activeFocusItem` rather than off what was clicked,
    // because "outside" is a property of the thing holding focus, not of the
    // thing under the cursor — every background, header and gap would
    // otherwise need its own handler. A press inside the focus item's own
    // rectangle is left alone; anything else clears.
    function dropStrayFocus(scenePos) {
        var item = Window.activeFocusItem
        if (!item)
            return
        var p = item.mapFromItem(null, scenePos)
        if (p.x >= 0 && p.y >= 0 && p.x <= item.width && p.y <= item.height)
            return
        // persistentSelection keeps a message highlighted after it loses focus
        // — that is what lets the context menu copy it, and it also means
        // dropping focus alone would leave the highlight behind.
        if (item.deselect)
            item.deselect()
        page.forceActiveFocus()
    }

    // Deliberately the topmost child and a MouseArea rather than a TapHandler
    // on the root: a handler on an ancestor never saw the press at all, the
    // list and the message text having taken it first. This sits above
    // everything, looks at the press, and then refuses it — `accepted = false`
    // sends the whole sequence on to whatever is underneath, so scrolling,
    // selection and every button still behave exactly as before.
    MouseArea {
        anchors.fill: parent
        z: 1000
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onPressed: (mouse) => {
            page.dropStrayFocus(mapToItem(null, mouse.x, mouse.y))
            mouse.accepted = false
        }
    }

    // ---- entry points for the global menu bar ---------------------------
    // Show Times is reachable from three places — both context menus and the
    // View menu — so the flag lives in one spot and everything aliases to it
    // rather than keeping its own copy to fall out of sync.
    property alias showTimes: msgArea.showTimes

    // The menu lives on the QMainWindow (see qtui/app.py) and reaches the
    // scene through Main.qml's menuAction(), which forwards to these.

    function focusSearch() {
        searchField.forceActiveFocus()
        searchField.selectAll()
    }

    function focusComposer() {
        if (msgArea.hasThread)
            composeField.forceActiveFocus()
    }

    // Same confirmation the sidebar's context menu uses — clearing is
    // local-only but still looks like deletion, so it always asks.
    function clearCurrentConversation() {
        if (threadStore.currentKey === "")
            return
        confirmDelete.threadKey = threadStore.currentKey
        confirmDelete.threadName = threadStore.peerName
        confirmDelete.open()
    }

    // Deleting a thread is destructive from the user's point of view even
    // though it only clears it locally, so confirm and say so explicitly.
    Dialog {
        id: confirmDelete
        property string threadKey: ""
        property string threadName: ""

        anchors.centerIn: parent
        modal: true
        title: "Delete conversation?"
        standardButtons: Dialog.Cancel | Dialog.Discard
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        onDiscarded: {
            threadStore.deleteThread(threadKey)
            close()
        }

        contentItem: ColumnLayout {
            spacing: 6
            Text {
                text: "Remove “" + confirmDelete.threadName + "” from this list?"
                color: Theme.text
                font.pixelSize: 13
                wrapMode: Text.Wrap
                Layout.maximumWidth: 330
            }
            Text {
                text: "This only clears it here — nothing is deleted on your "
                      + "iPhone, and a new message will bring it back."
                color: Theme.textDim
                font.pixelSize: 11
                wrapMode: Text.Wrap
                Layout.maximumWidth: 330
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        // ---- left: pinned grid + thread list ---------------------------
        // Floating card rather than a full-height panel: inset on all
        // sides, rounded, with a soft shadow so it reads as sitting above
        // the conversation.
        Rectangle {
            id: sidebar
            Layout.preferredWidth: page.sidebarVisible ? 292 : 0
            Layout.fillHeight: true
            Layout.margins: 10
            Layout.rightMargin: 4
            clip: true
            radius: 14
            color: Theme.sidebarBg
            visible: Layout.preferredWidth > 0

            layer.enabled: true
            layer.effect: MultiEffect {
                shadowEnabled: true
                shadowColor: "black"
                shadowOpacity: 0.35
                shadowBlur: 0.6
                shadowVerticalOffset: 2
            }

            Behavior on Layout.preferredWidth {
                NumberAnimation { duration: Theme.animBase; easing.type: Easing.OutCubic }
            }

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                // The window's nav row lives here, at the top of the
                // sidebar — traffic lights and the hamburger, nothing else.
                WindowChrome {
                    Layout.fillWidth: true
                    pageIndex: page.pageIndex
                    onPageRequested: (i) => page.pageRequested(i)
                }

                // Search — not wired up yet.
                Rectangle {
                    Layout.fillWidth: true
                    Layout.leftMargin: 10
                    Layout.rightMargin: 10
                    Layout.topMargin: 2
                    Layout.bottomMargin: 8
                    Layout.preferredHeight: 30
                    radius: height / 2
                    color: Qt.rgba(1, 1, 1, 0.07)
                    border.width: 1
                    border.color: Theme.separator

                    // Anchored rather than a Row: TextField carries its
                    // own left padding, which pushed the placeholder away
                    // from the icon and left them looking unrelated.
                    Icon {
                        id: searchIconBox
                        name: "search"
                        size: 14
                        color: Theme.textDim
                        anchors {
                            left: parent.left
                            leftMargin: 11
                            verticalCenter: parent.verticalCenter
                        }
                    }

                    TextField {
                        id: searchField
                        anchors {
                            left: searchIconBox.right
                            leftMargin: 7
                            right: parent.right
                            rightMargin: 10
                            verticalCenter: parent.verticalCenter
                        }
                        height: parent.height
                        leftPadding: 0
                        rightPadding: 0
                        topPadding: 0
                        bottomPadding: 0
                        background: null
                        color: Theme.text
                        placeholderText: "Search"
                        placeholderTextColor: Theme.textDim
                        font.pixelSize: 12
                        verticalAlignment: TextInput.AlignVCenter
                    }
                }

                // Pinned conversations, avatars only — the iOS grid. Capped
                // at nine by the store, which wraps to three rows here.
                Item {
                    id: pinnedFlow
                    Layout.fillWidth: true
                    Layout.topMargin: 12
                    Layout.bottomMargin: 6
                    visible: threadStore.pinnedCount > 0
                    implicitHeight: visible ? pinnedGrid.height : 0

                    // Centre the block as a whole: lay the tiles out in a
                    // grid sized to its own columns, then centre that.
                    Grid {
                        id: pinnedGrid
                        anchors.horizontalCenter: parent.horizontalCenter
                        spacing: 10

                        // Tiles grow to fill the sidebar. Up to three across
                        // like iOS, so a handful of pins get large avatars
                        // and a full nine still fit in three tidy rows.
                        readonly property int available: pinnedFlow.width - 20
                        columns: Math.max(1, Math.min(3, threadStore.pinnedCount))
                        readonly property real tile:
                            (available - spacing * (columns - 1)) / columns
                        // Tiles still divide the full width evenly — the
                        // avatar just sits smaller inside its tile, so the
                        // spacing stays uniform edge to edge.
                        readonly property int avatarSize:
                            Math.max(36, Math.min(Math.round(tile) - 18, 64))

                        // Avatar + label + breathing room. The padding is not
                        // decorative: the highlight is inset by 2, and the
                        // avatar grows 6% on hover, so a tile sized to its
                        // contents put both against the rounded corner.
                        readonly property int padTop: 12
                        readonly property int padBottom: 9
                        readonly property real cellH:
                            avatarSize + 22 + padTop + padBottom

                        // Drag state. dragIndex is the tile being carried,
                        // targetIndex the slot it would land in; every other
                        // tile renders at the slot it gets pushed into, so the
                        // grid shows the result before the drop commits.
                        property int dragIndex: -1
                        property int targetIndex: -1

                        function cellX(i) {
                            return (i % columns) * (Math.round(tile) + spacing)
                        }
                        function cellY(i) {
                            return Math.floor(i / columns) * (cellH + spacing)
                        }
                        // Inverse of the two above: which slot a point is over.
                        function slotAt(px, py) {
                            var col = Math.floor(px / (Math.round(tile) + spacing))
                            var row = Math.floor(py / (cellH + spacing))
                            col = Math.max(0, Math.min(columns - 1, col))
                            row = Math.max(0, row)
                            return Math.max(0, Math.min(pinnedRepeater.count - 1,
                                                        row * columns + col))
                        }

                    Repeater {
                        id: pinnedRepeater
                        // The user's own pin order, not the sidebar's recency
                        // order — the grid is arrangeable by drag.
                        model: threadStore.pinnedThreads

                        delegate: Item {
                            id: pinTile
                            required property int index
                            required property var modelData

                            readonly property string threadKey: modelData.threadKey
                            readonly property bool selected:
                                threadStore.currentKey === threadKey
                            readonly property bool dragging:
                                pinnedGrid.dragIndex === index

                            // Where this tile belongs on screen: its own slot
                            // normally, shifted by one while a drag passes
                            // over it.
                            readonly property int effectiveIndex: {
                                var d = pinnedGrid.dragIndex
                                var t = pinnedGrid.targetIndex
                                if (d < 0 || t < 0)
                                    return index
                                if (index === d)
                                    return t
                                if (d < t)
                                    return (index > d && index <= t) ? index - 1 : index
                                return (index >= t && index < d) ? index + 1 : index
                            }

                            width: Math.round(pinnedGrid.tile)
                            height: pinnedGrid.cellH

                            // The Grid positions this outer Item; `content`
                            // floats inside it, so dragging never fights the
                            // positioner over the cell's x/y.
                            Item {
                                id: content
                                width: parent.width
                                height: parent.height
                                z: pinTile.dragging ? 10 : 0
                                opacity: pinTile.dragging ? 0.85 : 1.0
                                scale: pinTile.dragging ? 1.04 : 1.0

                                x: pinnedGrid.cellX(pinTile.effectiveIndex)
                                   - pinnedGrid.cellX(pinTile.index)
                                y: pinnedGrid.cellY(pinTile.effectiveIndex)
                                   - pinnedGrid.cellY(pinTile.index)

                                // Only the tiles being pushed aside animate:
                                // the dragged one must track the cursor with
                                // no lag, and its bindings are broken anyway.
                                Behavior on x {
                                    enabled: !pinTile.dragging
                                    NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic }
                                }
                                Behavior on y {
                                    enabled: !pinTile.dragging
                                    NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic }
                                }
                                Behavior on scale {
                                    NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic }
                                }

                            // Behind the Column, so the avatar and label paint
                            // on top. Selection has to key off the store's open
                            // thread: the grid is a Repeater and has no
                            // currentIndex of its own like the list below does.
                            Rectangle {
                                anchors.fill: parent
                                anchors.margins: 2
                                radius: Theme.radiusRow
                                color: pinTile.selected
                                       ? Theme.selection
                                       : (pinMouse.containsMouse || pinTile.dragging
                                          ? Qt.rgba(1, 1, 1, 0.06) : "transparent")
                                Behavior on color { ColorAnimation { duration: Theme.animFast } }
                            }

                            Column {
                                anchors.top: parent.top
                                anchors.topMargin: pinnedGrid.padTop
                                anchors.horizontalCenter: parent.horizontalCenter
                                spacing: 5

                                Avatar {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    size: pinnedGrid.avatarSize
                                    source: pinTile.modelData.avatar
                                    initials: pinTile.modelData.initials
                                    scale: pinMouse.containsMouse ? 1.06 : 1.0
                                    Behavior on scale {
                                        NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic }
                                    }
                                }
                                // Name, with an unread dot beside it. A Row so
                                // the pair stays centred under the avatar as
                                // the dot comes and goes — the name alone
                                // would sit off-centre once a dot appeared
                                // next to it.
                                Row {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    spacing: 4
                                    readonly property bool unread:
                                        pinTile.modelData.unread > 0

                                    Rectangle {
                                        width: 6; height: 6; radius: 3
                                        color: Theme.accent
                                        visible: parent.unread
                                        anchors.verticalCenter: parent.verticalCenter
                                    }

                                    Text {
                                        // Leaves room for the dot when there
                                        // is one, so a long name elides
                                        // instead of pushing it out of view.
                                        width: Math.round(pinnedGrid.tile) - 4
                                               - (parent.unread ? 10 : 0)
                                        horizontalAlignment: Text.AlignHCenter
                                        elide: Text.ElideRight
                                        text: pinTile.modelData.name
                                        color: pinTile.selected
                                               ? Qt.rgba(1, 1, 1, 0.9) : Theme.textDim
                                        font.pixelSize: pinnedGrid.avatarSize > 60 ? 12 : 11
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                }
                            }

                            MouseArea {
                                id: pinMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                cursorShape: pinTile.dragging
                                             ? Qt.ClosedHandCursor : Qt.ArrowCursor
                                drag.target: content
                                drag.axis: Drag.XAndYAxis
                                drag.threshold: 6
                                drag.smoothed: false

                                // Set once the pointer passes the drag
                                // threshold: `clicked` still fires after a
                                // drag, and without this every reorder would
                                // also open a conversation.
                                property bool moved: false

                                function restoreBindings() {
                                    // Dragging assigned content.x/y directly,
                                    // which broke their bindings — without
                                    // this the tile stays where it was dropped
                                    // and the reorder shifts it a second time.
                                    content.x = Qt.binding(function () {
                                        return pinnedGrid.cellX(pinTile.effectiveIndex)
                                             - pinnedGrid.cellX(pinTile.index)
                                    })
                                    content.y = Qt.binding(function () {
                                        return pinnedGrid.cellY(pinTile.effectiveIndex)
                                             - pinnedGrid.cellY(pinTile.index)
                                    })
                                }

                                onPressed: (m) => {
                                    moved = false
                                    if (m.button === Qt.LeftButton) {
                                        pinnedGrid.dragIndex = pinTile.index
                                        pinnedGrid.targetIndex = pinTile.index
                                    }
                                }
                                onPositionChanged: {
                                    if (!drag.active)
                                        return
                                    moved = true
                                    // Centre of the floating tile in grid
                                    // coordinates: its own slot, plus however
                                    // far the drag has carried it.
                                    var cx = pinTile.x + content.x + pinTile.width / 2
                                    var cy = pinTile.y + content.y + pinTile.height / 2
                                    pinnedGrid.targetIndex = pinnedGrid.slotAt(cx, cy)
                                }
                                onReleased: {
                                    if (pinnedGrid.dragIndex < 0)
                                        return
                                    var from = pinnedGrid.dragIndex
                                    var to = pinnedGrid.targetIndex
                                    pinnedGrid.dragIndex = -1
                                    pinnedGrid.targetIndex = -1
                                    restoreBindings()
                                    if (moved && from !== to)
                                        threadStore.movePin(from, to)
                                }
                                onCanceled: {
                                    pinnedGrid.dragIndex = -1
                                    pinnedGrid.targetIndex = -1
                                    restoreBindings()
                                }
                                onClicked: (m) => {
                                    if (moved)
                                        return
                                    if (m.button === Qt.RightButton)
                                        threadStore.togglePin(pinTile.threadKey)
                                    else
                                        threadStore.openThread(pinTile.threadKey)
                                }
                            }
                            }
                        }
                    }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    color: Theme.separator
                    visible: pinnedFlow.visible
                }

                SmoothListView {
                    id: threadList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.topMargin: 6
                    clip: true
                    spacing: 2
                    model: threadStore.threadModel
                    // The store restores the newest thread on launch.
                    currentIndex: 0
                    // No visible scrollbar in the conversation list.
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AlwaysOff }

                    // A model reset (new page, thread added/removed) still
                    // drops contentY to 0. Put it back so the list doesn't
                    // jump to the top under the user.
                    property real savedY: 0
                    Connections {
                        target: threadStore.threadModel
                        function onModelAboutToBeReset() {
                            threadList.savedY = threadList.contentY
                        }
                        function onModelReset() {
                            Qt.callLater(function () {
                                threadList.contentY = Math.max(
                                    0, Math.min(threadList.savedY,
                                                Math.max(0, threadList.contentHeight
                                                            - threadList.height)))
                            })
                        }
                    }

                    // Infinite scroll: pull in the next page as the end
                    // of the list comes into view. Guarded on the row count
                    // so it fires once per page — contentYChanged runs every
                    // frame while scrolling, and asking for another page on
                    // each one reset the model repeatedly mid-scroll.
                    property int _lastPageRequest: 0
                    onContentYChanged: {
                        if (contentHeight > 0
                                && contentY + height > contentHeight - 400
                                && count !== _lastPageRequest) {
                            _lastPageRequest = count
                            threadStore.loadMoreThreads()
                        }
                    }

                    delegate: Item {
                        id: threadDelegate
                        required property int index
                        required property string threadKey
                        required property string name
                        required property string preview
                        required property string stamp
                        required property int unread
                        required property string avatar
                        required property string initials
                        required property bool pinned

                        // Selection follows the store, not `currentIndex`:
                        // opening a chat from the pinned grid never touches
                        // the list's index, so an index-based highlight left
                        // the previously opened row lit alongside the pin.
                        readonly property bool selected:
                            threadStore.currentKey === threadKey

                        // A pinned conversation moves into the grid above
                        // rather than appearing in both places.
                        width: threadList.width
                        visible: !pinned
                        height: pinned ? 0 : 58

                        Rectangle {
                            anchors {
                                fill: parent
                                leftMargin: 6; rightMargin: 6
                                topMargin: 1; bottomMargin: 1
                            }
                            radius: Theme.radiusRow
                            color: threadDelegate.selected
                                   ? Theme.selection
                                   : (rowMouse.containsMouse
                                      ? Qt.rgba(1, 1, 1, 0.06) : "transparent")

                            Behavior on color { ColorAnimation { duration: Theme.animFast } }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 4
                                anchors.rightMargin: 10
                                spacing: 8

                                // Unread dot in the gutter, left of the avatar.
                                Rectangle {
                                    Layout.preferredWidth: 8
                                    Layout.preferredHeight: 8
                                    radius: 4
                                    color: Theme.accent
                                    opacity: unread > 0 ? 1 : 0
                                    Behavior on opacity { NumberAnimation { duration: Theme.animBase } }
                                }

                                Avatar {
                                    size: 40
                                    source: threadDelegate.avatar
                                    initials: threadDelegate.initials
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 1

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 6
                                        Text {
                                            Layout.fillWidth: true
                                            text: threadDelegate.name
                                            elide: Text.ElideRight
                                            color: Theme.text
                                            font.pixelSize: 13
                                            font.weight: Font.DemiBold
                                        }
                                        Text {
                                            text: threadDelegate.stamp
                                            color: threadDelegate.selected
                                                   ? Qt.rgba(1, 1, 1, 0.8) : Theme.textDim
                                            font.pixelSize: 11
                                        }
                                    }
                                    // Single line: wrapping here reserved a
                                    // second line's height even for short
                                    // previews, which read as a stray gap.
                                    Text {
                                        Layout.fillWidth: true
                                        text: threadDelegate.preview
                                        elide: Text.ElideRight
                                        color: threadDelegate.selected
                                               ? Qt.rgba(1, 1, 1, 0.8) : Theme.textDim
                                        font.pixelSize: 12
                                    }
                                }
                            }

                            MouseArea {
                                id: rowMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                onClicked: (m) => {
                                    if (m.button === Qt.RightButton) {
                                        rowMenu.popup()
                                    } else {
                                        threadList.currentIndex = threadDelegate.index
                                        threadStore.openThread(threadDelegate.threadKey)
                                    }
                                }

                                Menu {
                                    id: rowMenu
                                    MenuItem {
                                        text: threadDelegate.pinned ? "Unpin" : "Pin"
                                        onTriggered: threadStore.togglePin(
                                            threadDelegate.threadKey)
                                    }
                                    MenuSeparator {}
                                    MenuItem {
                                        text: "Delete Conversation"
                                        onTriggered: {
                                            confirmDelete.threadKey =
                                                threadDelegate.threadKey
                                            confirmDelete.threadName =
                                                threadDelegate.name
                                            confirmDelete.open()
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // No divider — the sidebar floats, so its own edge separates it.

        // ---- right: peer header + bubbles + compose --------------------
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // Messages, with the peer header floating over them: no hard
            // rule, just a fade so bubbles dissolve as they scroll under it.
            Item {
                id: msgArea
                Layout.fillWidth: true
                Layout.fillHeight: true

                readonly property bool hasThread: threadStore.peerName !== ""
                readonly property int headerHeight: 70

                // Show Times, as in Messages.app: the whole right-hand side of
                // the conversation slides left to open a gutter, and every
                // message — outgoing and incoming alike — gets the time it was
                // sent in it.
                //
                // Held here rather than per-delegate so it survives delegates
                // being destroyed and rebuilt as you scroll, and so the right
                // gutter is one number the whole thread agrees on.
                property bool showTimes: false
                // Wide enough for the longest stamp ("12:24 PM" is 48px at
                // this size) plus the margin outside it and a clear gap
                // between the stamp and the bubble it belongs to.
                readonly property int timeGutter: 74

                // Empty state
                Text {
                    anchors.centerIn: parent
                    visible: !msgArea.hasThread
                    text: "Select a conversation"
                    color: Theme.textDim
                    font.pixelSize: 18
                }

            // Right-click anywhere in the conversation, not only on a
            // bubble. Declared before the list so it sits beneath it: the
            // Flickable takes the left button for dragging and lets the right
            // one through, and a bubble that wants its own menu has a
            // MouseArea of its own further up the stack.
            MouseArea {
                anchors.fill: parent
                visible: msgArea.hasThread
                acceptedButtons: Qt.RightButton
                onClicked: (mouse) => {
                    chatMenu.x = mouse.x
                    chatMenu.y = mouse.y
                    chatMenu.open()
                }

                Menu {
                    id: chatMenu
                    MenuItem {
                        text: msgArea.showTimes ? "Hide Times" : "Show Times"
                        onTriggered: msgArea.showTimes = !msgArea.showTimes
                    }
                }
            }

            SmoothListView {
                id: msgList
                anchors.fill: parent
                visible: msgArea.hasThread
                // Leave room so the first bubble starts below the header
                // rather than under it.
                topMargin: msgArea.headerHeight + 30
                // Height of the dissolve above the composer. The mask reads it.
                //
                // Kept under the caption line's 14px so the fade cannot reach
                // the "Delivered"/"Read" text, which sits in that band right
                // above the resting edge and was being dimmed out by a deeper
                // fade. The two are related: anything taller than the caption
                // either swallows it or has to be pushed clear by a margin
                // that reads as a gap above the composer.
                // A new bubble rises into place instead of appearing fully
                // formed, and anything above it glides rather than jumping.
                //
                // Only real insertions animate. Loading a thread, importing a
                // backup and every rebuild go through `reload()`, which resets
                // the model — and a reset raises no add transition, so opening
                // a conversation does not try to animate 4000 bubbles.
                // Fades in while rising the last few pixels into place. The
                // rise is `riseOffset`, a transform on the delegate rather
                // than its `y` — see the comment there for why animating the
                // layout position instead is what read as jitter.
                add: Transition {
                    SequentialAnimation {
                        // Marks the bubble as still arriving. A receipt can
                        // land inside this window — a message that is
                        // delivered almost as fast as it is sent — and then
                        // the caption's own fade-up would run at the same
                        // time as the bubble's, two overlapping animations on
                        // the same few pixels. While this is set, the caption
                        // skips its animation and simply arrives with the
                        // bubble it belongs to, as one movement.
                        PropertyAction { property: "freshlyAdded"; value: true }
                        ParallelAnimation {
                            NumberAnimation {
                                property: "opacity"; from: 0; to: 1
                                duration: Theme.animArrive
                                easing.type: Easing.OutCubic
                            }
                            NumberAnimation {
                                property: "riseOffset"; from: 10; to: 0
                                duration: Theme.animArrive
                                easing.type: Easing.OutCubic
                            }
                        }
                        PropertyAction { property: "freshlyAdded"; value: false }
                    }
                }
                // Rows pushed along by the insertion. Without this they snap
                // to their new positions while the new bubble eases into its
                // own, which is the jitter the animation exists to remove.
                displaced: Transition {
                    NumberAnimation {
                        properties: "y"
                        duration: Theme.animBase
                        easing.type: Easing.OutCubic
                    }
                }

                readonly property int bottomFade: 6
                // Deliberately smaller than the fade rather than equal to it.
                // Matching them kept the newest bubble entirely clear of the
                // dissolve, but that is what left the thread resting visibly
                // short of the bottom. At 3 the fade laps the last few pixels
                // of the bubble — which is its rounded padding, not its text —
                // and the conversation sits where it should.
                bottomMargin: 3
                clip: true
                spacing: 2
                model: threadStore.messageModel

                // Message bubbles are all different heights, so ListView can
                // only *estimate* contentHeight from the delegates it has
                // built. Every delegate created while scrolling corrects that
                // estimate, which shifts contentY under the pointer — the
                // jitter. Building a screenful either side means heights are
                // already known by the time they matter.
                cacheBuffer: 1200

                // `maxContentY` and the mouse-wheel handling both come from
                // SmoothListView. This used to redeclare both here, which is
                // what made the view jump to the top of the conversation:
                //
                // The local `maxContentY` shadowed the base one but computed
                // a different value — it ignored topMargin and bottomMargin,
                // so the two disagreed about where the bottom is by the
                // height of the header. Worse, only the checks written here
                // saw the shadow; scrollBy() and smoothScrollToEnd() inside
                // SmoothListView kept using the base. And when a contentHeight
                // estimate momentarily came in under `height` — routine with
                // variable-height bubbles, and most likely right after
                // appending a message — the local formula collapsed to
                // `originY`, so clamping against it slammed contentY to the
                // very top.
                //
                // The second WheelHandler was the other half: both it and
                // SmoothListView's accepted the same notch, one writing
                // contentY directly while the other drove the integrator
                // toward a target, each cancelling the other mid-scroll.
                //
                // Rubber-banding at the ends compounds the estimate shifting,
                // so the overshoot stays off here — but via SmoothListView's
                // own flag, so its wheel path honours it too rather than
                // springing toward a target StopAtBounds won't let it reach.
                boundsBehavior: Flickable.StopAtBounds
                bounceOnWheel: false
                // See `smoothWheel` in SmoothListView: the eased chase cannot
                // track this list, because realizing message delegates keeps
                // moving `originY` out from under its target.
                smoothWheel: false

                // Typing indicator: an incoming-side bubble with three
                // pulsing dots, sitting where their next message will land.
                // A footer rather than a model row so it can't be confused
                // for a message or scrolled past independently.
                footer: Item {
                    width: msgList.width
                    height: threadStore.peerTyping ? 34 : 0
                    visible: height > 0
                    Behavior on height {
                        NumberAnimation { duration: Theme.animBase; easing.type: Easing.OutCubic }
                    }

                    Rectangle {
                        id: typingBubble
                        anchors.left: parent.left
                        anchors.leftMargin: 14
                        anchors.verticalCenter: parent.verticalCenter
                        width: 52
                        height: 26
                        radius: 13
                        color: Theme.bubbleIn
                        opacity: threadStore.peerTyping ? 1 : 0
                        Behavior on opacity { NumberAnimation { duration: Theme.animBase } }

                        Row {
                            anchors.centerIn: parent
                            spacing: 4
                            Repeater {
                                model: 3
                                Rectangle {
                                    required property int index
                                    width: 7; height: 7; radius: 3.5
                                    color: Theme.textDim

                                    // Staggered so the dots ripple rather
                                    // than blink in unison.
                                    SequentialAnimation on opacity {
                                        running: threadStore.peerTyping
                                        loops: Animation.Infinite
                                        PauseAnimation { duration: index * 160 }
                                        NumberAnimation { to: 1.0; duration: 300 }
                                        NumberAnimation { to: 0.35; duration: 300 }
                                        PauseAnimation { duration: (2 - index) * 160 }
                                    }
                                }
                            }
                        }
                    }
                }

                // Overlay scrollbar with no groove or divider line — the
                // default style draws a track with a border, which read as
                // a stray vertical rule beside the messages.
                ScrollBar.vertical: ScrollBar {
                    id: msgScroll
                    policy: ScrollBar.AsNeeded
                    width: 8
                    anchors.right: parent.right
                    anchors.rightMargin: 2
                    background: null
                    contentItem: Rectangle {
                        implicitWidth: 6
                        radius: 3
                        color: Qt.rgba(1, 1, 1, msgScroll.pressed ? 0.45
                                                : (msgScroll.hovered ? 0.32 : 0.18))
                        opacity: msgScroll.active ? 1 : 0
                        Behavior on opacity {
                            NumberAnimation { duration: Theme.animBase }
                        }
                        Behavior on color { ColorAnimation { duration: Theme.animFast } }
                    }
                }

                // One gradient mask across the whole list, so content
                // dissolves per-pixel as it scrolls under the floating
                // header. OpacityMask (Qt5Compat) rather than MultiEffect:
                // MultiEffect's mask silently did nothing here.
                // No layer.samples: the layer is a full-viewport FBO
                // redrawn every frame while scrolling, and multisampling it
                // was costing more than it bought. Bubbles are drawn by
                // Shape's curve renderer, which antialiases analytically,
                // and text and images bring their own.
                layer.enabled: true
                layer.smooth: true
                layer.effect: OpacityMask {
                    maskSource: Item {
                        width: msgList.width
                        height: msgList.height
                        Rectangle {
                            anchors.fill: parent
                            gradient: Gradient {
                                GradientStop { position: 0.0; color: "transparent" }
                                GradientStop {
                                    position: Math.min(
                                        0.9, (msgArea.headerHeight + 58)
                                             / Math.max(1, msgList.height))
                                    color: "white"
                                }
                                // Mirror of the header fade, thinner: bubbles
                                // dissolve on their way down to the composer
                                // instead of meeting it at a hard edge.
                                //
                                // `bottomMargin` on the list is what keeps the
                                // newest message out of it — resting at the
                                // end leaves exactly the fade's height of
                                // empty space below the last bubble, so the
                                // fade only ever bites while scrolling back
                                // through history.
                                GradientStop {
                                    position: 1.0 - Math.min(
                                        0.3, msgList.bottomFade
                                             / Math.max(1, msgList.height))
                                    color: "white"
                                }
                                GradientStop { position: 1.0; color: "transparent" }
                            }
                        }
                    }
                }

                // A model reset drops contentY to 0, throwing the view to the
                // very top of the conversation. The message model resets on
                // any rebuild — and sending resets it twice over: the new row
                // changes the count, then the daemon's echo comes back and
                // `_upgrade_echo_guid` rebuilds to bind the guid. That is why
                // the jump was easiest to trigger by pressing send.
                //
                // Saved as distance from the *bottom*, not absolute contentY,
                // because a conversation is bottom-anchored: sitting at the
                // newest message has to stay there even though the content
                // just got taller, and sitting N px up in history has to stay
                // N px up.
                // Not restored when the reset is a *different conversation*
                // loading: the saved offset describes the thread being left,
                // so applying it to the new one lands at an arbitrary point
                // in its history instead of at the newest message.
                //
                // Told apart by which thread the current rows belong to, not
                // by `justOpened`: `_open` sets the key and rebuilds the model
                // *before* emitting peerChanged, so at reset time the flag is
                // still showing the previous thread's state. `renderedKey` is
                // only updated once rows for a thread are actually in place.
                property real savedBottom: -1
                property string renderedKey: ""
                Connections {
                    target: threadStore.messageModel
                    function onModelAboutToBeReset() {
                        msgList.savedBottom =
                            msgList.renderedKey === threadStore.currentKey
                            ? msgList.maxContentY - msgList.contentY
                            : -1
                    }
                    function onModelReset() {
                        var saved = msgList.savedBottom
                        msgList.savedBottom = -1
                        msgList.renderedKey = threadStore.currentKey
                        if (saved < 0)
                            return
                        // Restoring to the bottom is `landAtEnd`'s job, not
                        // arithmetic against a figure that is about to change:
                        // `maxContentY` at reset time and after it are two
                        // different numbers, and subtracting across them is
                        // how a send could land at the very top.
                        if (saved < 8) {
                            msgList.landAtEnd()
                            return
                        }
                        Qt.callLater(function () {
                            msgList.contentY = Math.max(
                                msgList.minContentY,
                                Math.min(msgList.maxContentY,
                                         msgList.maxContentY - saved))
                        })
                    }
                }

                // Landing at the end of a long thread takes several attempts.
                // `contentHeight` is only an estimate until every delegate has
                // been built, and with message bubbles it does not converge
                // quietly: measured on a real thread it went 96k → 116k → 76k
                // px while opening. So a single positionViewAtEnd() aims at a
                // figure that is already wrong, and the view stops somewhere
                // arbitrary — 2000px up the conversation, looking random.
                //
                // Re-aim every frame until it sticks. Bounded by a tick count
                // so a thread whose height never settles can't hold the view.
                // This loop only re-aims the *target* of the eased scroll; the
                // motion itself is the Behavior on contentY, which Qt advances
                // once per rendered frame and so already runs at the display's
                // refresh rate. A FrameAnimation here would tick at that rate
                // too, but it does not tick at all without a render loop,
                // which costs the whole offscreen test harness for no visible
                // gain — the easing interpolates between re-aims regardless.
                //
                // 8ms rather than 16 so retargeting stays finer than a frame
                // even at 165Hz. It only runs during a settle, ~150ms at a
                // time.
                //
                // Thresholds are in seconds, not frame counts: "six frames"
                // means 100ms at 60Hz and 36ms at 165Hz, so a frame-counted
                // budget silently gets stricter the better the monitor is.
                Timer {
                    id: settleToEnd
                    interval: 8
                    repeat: true
                    property real stableTime: 0
                    property real ranTime: 0
                    property real lastY: NaN
                    onTriggered: {
                        msgList.aimAtEnd()
                        var y = msgList.contentY
                        if (Math.abs(msgList.maxContentY - y) < 1 && y === lastY)
                            stableTime += interval / 1000.0
                        else
                            stableTime = 0
                        lastY = y
                        // Reveal as soon as it has held the bottom briefly,
                        // rather than waiting for the whole run to finish. The
                        // correction being hidden happens immediately;
                        // everything after it is sub-pixel, and waiting for a
                        // full stop left the conversation blank for noticeably
                        // longer than it needed to be.
                        if (stableTime >= 0.05) {
                            msgList.landed = true
                            // Landed on its own, so the backstop has nothing
                            // left to do — and firing anyway put a 3px jump
                            // into an already-visible conversation 700ms
                            // after it opened.
                            revealGuard.stop()
                        }
                        // Hold the bottom for a stretch rather than stopping
                        // at the first frame that looks settled. A bubble
                        // appended a moment ago has not necessarily been
                        // measured yet, so one good frame proves nothing —
                        // that is what left the view short of the bottom
                        // after a new message arrived.
                        ranTime += interval / 1000.0
                        if (stableTime >= 0.1 || ranTime > 1.5)
                            running = false
                    }
                    // Deliberately no onRunningChanged reveal: restart() is
                    // stop-then-start, so it fired with running == false and
                    // revealed the thread before a single aim had happened.
                    // `revealGuard` is the backstop, and it lands the view
                    // first.
                }

                // `positionViewAtEnd` alone is not the end of the scroll.
                // It rests the last delegate flush against the viewport
                // bottom, which leaves contentY a whole `bottomMargin` short
                // of `maxContentY` — so none of that margin is showing, the
                // delivery caption sits hard on the bottom edge, and the fade
                // that lives in the margin laps into it.
                //
                // Taking the remaining pixels explicitly is also what lets the
                // settle timer below ever finish: its "arrived" test compares
                // against maxContentY, which positionViewAtEnd never reaches,
                // so every open previously ran the full tick budget.
                function jumpToEnd() {
                    positionViewAtEnd()
                    contentY = maxContentY
                }

                // The animated counterpart. Assignment only — deliberately no
                // `positionViewAtEnd`, because that writes contentY from C++
                // and a Behavior only sees writes made from QML. Calling it
                // here snapped the view on every settle tick and then eased
                // the few pixels left over, which is what the glide felt like
                // it was fighting.
                function glideToEnd() {
                    contentY = maxContentY
                }

                // Every path that wants the bottom goes through here. There
                // must be exactly one writer of contentY at a time: while the
                // glide is in flight, an instant `jumpToEnd` from some other
                // path snaps the view forward and the easing curve then drags
                // it back to where it had got to — a jump followed by a
                // visible reversal.
                function aimAtEnd() {
                    if (animateScroll)
                        glideToEnd()
                    else
                        jumpToEnd()
                }

                // Opening a thread cannot land on the bottom in one go — the
                // height of a conversation is an estimate until its delegates
                // exist, so the first aim is a few pixels out and the settle
                // timer corrects it a frame or two later. That correction was
                // visible as a small jump.
                //
                // So don't show the thread until it has stopped moving. The
                // wait is a couple of frames, and a short fade makes it read
                // as the conversation arriving rather than as a blink.
                property bool landed: false
                opacity: landed ? 1 : 0
                // A conversation that never settles must still be shown. This
                // is the ceiling on how long the fade can hold it back,
                // whatever the layout is doing.
                Timer {
                    id: revealGuard
                    interval: 700
                    onTriggered: {
                        // Aim once more before showing it. Revealing blindly
                        // on a timer put the view on screen still thousands of
                        // pixels from the bottom on a long thread — the exact
                        // failure the fade exists to prevent, just later.
                        msgList.jumpToEnd()
                        msgList.landed = true
                    }
                }
                Behavior on opacity {
                    NumberAnimation {
                        // Only the fade *in* is animated. Fading out took
                        // 120ms during which the incoming thread was still on
                        // screen and still landing — measured on one thread it
                        // travelled 14760px while partly visible, in two jumps
                        // of 3141 and 10154px. That was the open jolt: not the
                        // settle being visible, but the previous thread's fade
                        // keeping the view lit while the next one searched for
                        // its bottom.
                        //
                        // A duration rather than `enabled`, because the
                        // animation reads its duration when it starts — after
                        // `landed` has already changed — where a binding on
                        // `enabled` races the opacity binding it guards.
                        duration: msgList.landed ? Theme.animFast : 0
                        easing.type: Easing.OutCubic
                    }
                }

                function landAtEnd() {
                    settleToEnd.stableTime = 0
                    settleToEnd.ranTime = 0
                    settleToEnd.lastY = NaN
                    settleToEnd.restart()
                    // The first aim honours the mode too, or the glide
                    // starts with the very snap it exists to avoid.
                    aimAtEnd()
                }

                // Same landing, but glided rather than snapped — for a message
                // arriving while you are already at the bottom, where the jump
                // is the thing that reads as jarring. Opening a thread stays
                // instant: animating that would scroll the whole history past
                // you every time you switch conversation.
                //
                // The eased scroll is a Behavior, not an animation with a
                // fixed target, precisely because the target moves: the new
                // bubble's height is not known when the scroll starts, so
                // `jumpToEnd` gets re-applied by the settle timer above and
                // each re-application retargets the same easing curve instead
                // of restarting a new one. That is what keeps it from
                // stuttering as the layout firms up.
                property bool animateScroll: false
                Behavior on contentY {
                    enabled: msgList.animateScroll
                    // Same duration as the bubble's own arrival, so the
                    // thread rising and the message fading up into place are
                    // one movement rather than two of different lengths.
                    NumberAnimation {
                        duration: Theme.animArrive
                        easing.type: Easing.OutCubic
                    }
                }
                Timer {
                    id: followRelease
                    interval: Theme.animArrive + 400
                    onTriggered: msgList.animateScroll = false
                }

                function followToEnd() {
                    animateScroll = true
                    followRelease.restart()
                    landAtEnd()
                }

                // Stick to the newest message as it arrives, but only when the
                // user is already near the bottom, so scrolling back through
                // history isn't yanked away when a message lands.
                //
                // Not `smoothScrollToEnd`: that routes through the eased
                // chase, which cannot track this list (see `smoothWheel`), so
                // an arriving message silently failed to scroll into view.
                onCountChanged: Qt.callLater(function () {
                    if (justOpened) {
                        justOpened = false
                        landAtEnd()
                        return
                    }
                    // Scrolled back through history? Leave the view alone.
                    if (maxContentY - contentY < height * 0.5)
                        followToEnd()
                })
                // On first load / thread switch, jump without animating.
                Component.onCompleted: Qt.callLater(landAtEnd)
                // Opening a thread should land at the bottom instantly.
                // Without this the animated follow below runs on the reload,
                // so a long conversation visibly scrolled itself down.
                property bool justOpened: false
                // Images load asynchronously, so when a thread opens their
                // height is still 0 and "scroll to the end" lands short.
                // Keep re-pinning while content grows, until the user
                // scrolls for themselves.
                property bool pinBottom: false
                // Only re-pin while the view is still down at the bottom.
                // Scrolling away is itself the signal to stop, which covers
                // input paths that raise no drag, flick or wheel signal.
                // No "already near the bottom" test here any more. It was
                // meant to stop a late image yanking the view, but that is
                // what `onUserScrolled` covers — and because the estimate can
                // be thousands of pixels out on open, the guard's real effect
                // was to abandon the pin exactly when it was needed and leave
                // the thread stranded mid-history.
                //
                // Past that window, growth still has to be followed — a
                // Delivered or Read caption appears under the last bubble
                // after the fact and makes the content taller beneath a view
                // already resting on the bottom. Nothing followed it, so the
                // receipt shoved the conversation up in a single frame.
                // Glide instead, so it reads like the new-message animation.
                property real lastMaxContentY: 0
                onContentHeightChanged: {
                    // The bottom before this growth. `atBottom` can't answer
                    // it: that is only recomputed when contentY moves, and
                    // what moved here is contentHeight.
                    var wasAtBottom = (lastMaxContentY - contentY) < 4
                    var delta = maxContentY - lastMaxContentY
                    lastMaxContentY = maxContentY
                    if (pinBottom) {
                        Qt.callLater(aimAtEnd)
                        return
                    }
                    if (!landed || !wasAtBottom || delta === 0)
                        return
                    // Content getting *shorter* is never a message; it is the
                    // list revising its own estimate as delegates outside the
                    // viewport are built and discarded. Measured on a 1.5k
                    // message thread it corrected by 4670px a couple of
                    // seconds after opening.
                    //
                    // Returning here left a bottom-resting view stranded up to
                    // 123px above the bottom, and the next growth then glided
                    // it back down — the open jolt: a drift up, then a slide
                    // back. Follow the shrink in the same frame instead, so
                    // the bottom never moves out from under the view.
                    if (delta < 0) {
                        aimAtEnd()
                        return
                    }
                    // Two quite different kinds of growth arrive here.
                    //
                    // A caption band opening under the last bubble grows a
                    // pixel or two per frame, because its own height is
                    // animated. The smooth motion is already being supplied
                    // by that animation, so all this has to do is stay welded
                    // to the bottom while it happens; easing on top would lag
                    // behind its own layout.
                    //
                    // A whole bubble appearing arrives in one step, and that
                    // is the one that needs the eased glide.
                    //
                    // `!animateScroll` guards the fast case: a message
                    // delivered almost as soon as it is sent brings its
                    // caption in while the bubble's glide is still running,
                    // and switching to an instant pin there killed the glide
                    // and snapped the rest of the distance in one frame.
                    // Staying in the glide simply retargets the same easing
                    // curve, which is what it is built to absorb.
                    if (delta < 40 && !animateScroll) {
                        jumpToEnd()
                    } else {
                        followToEnd()
                    }
                }

                // The composer grows as you add lines with Shift+Enter, which
                // shortens this view. Without following, the conversation
                // stayed where it was and the newest message slid up out of
                // sight behind the growing pill. Glided, not jumped, so it
                // rides along with the composer's own height animation and
                // reads as one movement.
                //
                // Only when you were already at the bottom: someone reading
                // history should keep their place while they type.
                property bool atBottom: true
                onContentYChanged: atBottom = (maxContentY - contentY) < 4
                onHeightChanged: {
                    if (landed && atBottom)
                        followToEnd()
                }
                // Any user scroll input releases the bottom pin — otherwise
                // a slowly-loading image growing contentHeight yanks the
                // view back down under them.
                onUserScrolled: {
                    pinBottom = false
                    settleToEnd.running = false
                    // Or the wheel would be easing too, which feels like drag.
                    animateScroll = false
                    followRelease.stop()
                }

                // Stop re-pinning a couple of seconds after opening, so a
                // slow image can't yank the view long afterwards.
                Timer {
                    id: pinRelease
                    interval: 2500
                    onTriggered: msgList.pinBottom = false
                }
                Connections {
                    target: threadStore
                    function onPeerChanged() {
                        msgList.justOpened = true
                        msgList.pinBottom = true
                        // Whatever the last thread was doing is over. A glide
                        // left running by a message that arrived just before
                        // the switch made the new thread *animate* its way to
                        // the bottom — measured at 10465px in one eased move,
                        // on screen, which is the jolt in its worst form.
                        msgList.animateScroll = false
                        followRelease.stop()
                        // Hidden until it has settled at the bottom; see
                        // `landed`. Only on a thread switch — a message
                        // arriving must never blank the conversation.
                        msgList.landed = false
                        revealGuard.restart()
                        pinRelease.restart()
                        msgList.landAtEnd()
                    }
                }

                delegate: Column {
                    id: msgDelegate
                    required property string body
                    required property bool outgoing
                    required property string reaction
                    required property string reactionEmoji
                    required property bool mediaOnly
                    required property string image
                    required property string fileLabel
                    required property string kind
                    required property string sender
                    required property string senderAvatar
                    required property string senderInitials
                    required property string divider
                    // Extra space above the bubble when there was a real pause
                    // before it. Zero for a burst, so quick back-and-forth
                    // still stacks tightly. See _RUN_GAP_SECONDS.
                    required property int gapBefore
                    required property bool tail
                    required property string richBody
                    required property bool jumbo
                    required property int imageW
                    required property int imageH
                    // iMessage's own id, empty for MAP messages. Gates every
                    // action the context menu offers — none of them can name
                    // their target without it.
                    required property string guid
                    // "", "delivered" or "read". Only ever set on the last
                    // outgoing bubble; see _rebuild_messages.
                    //
                    // Not called `state`: every QML Item already has one,
                    // and shadowing it breaks the state machine silently.
                    required property string deliveryState
                    required property string deliveryStamp
                    // "6:24 PM". Drawn only in the Show Times gutter.
                    required property string timeStamp
                    // Earlier texts of an edited message, oldest first.
                    required property var edits
                    // Non-empty when this message replies to another.
                    required property string replyBody
                    // Collapsed by default: the current text is the one that
                    // counts, and history would otherwise pad every edited
                    // message forever.
                    property bool showEdits: false

                    // Gap from the window edge to the bubble's right wall.
                    // The delivery caption aligns to this too — and so, with
                    // Show Times on, does the slide that opens the gutter:
                    // everything anchored to the right moves as one because
                    // they all measure from here.
                    //
                    // Animated on the delegate rather than on the shared
                    // property, so each row eases independently and the
                    // conversation opens the gutter as a wave rather than a
                    // single hard shift.
                    property int sideMargin: 22 + (msgArea.showTimes
                                                   ? msgArea.timeGutter : 0)
                    Behavior on sideMargin {
                        NumberAnimation {
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }

                    // Driven by the view's `add` transition, so it only ever
                    // moves for a genuine insertion — never for a delegate
                    // being created because you scrolled it into view.
                    //
                    // A transform, not `y`: animating the layout position is
                    // what made this jitter before, because the eased contentY
                    // glide is moving the same bubble at the same time and the
                    // two rates fought. A transform is applied after layout,
                    // so the scroll maths never sees it and the two motions
                    // simply add.
                    property real riseOffset: 0
                    transform: Translate { y: msgDelegate.riseOffset }
                    // True only while the add transition above is running.
                    property bool freshlyAdded: false

                    // Media stands on its own: a photo is a rounded image
                    // with no bubble behind it, a sticker is bare and
                    // unrounded. Any accompanying text is its own bubble.
                    // Group threads keep a gutter on the left for the
                    // sender's avatar; 1:1 threads don't need one.
                    readonly property bool inGroup:
                        threadStore.peerIsGroup && !outgoing
                    readonly property int gutter: inGroup ? 40 : 0

                    readonly property bool isImage: kind === "image"
                    readonly property bool isSticker: kind === "sticker"
                    readonly property bool isMedia: isImage || isSticker
                    // An emoji-only message is drawn big and bare. It still
                    // uses the bubble item for layout — only the painted
                    // background and the tail drop away.
                    readonly property bool isJumbo: jumbo && !isMedia

                    width: msgList.width
                    spacing: 0
                    // Padding rather than ListView spacing: spacing is one
                    // value for the whole list, and this has to vary per
                    // message.
                    topPadding: gapBefore

                    // Outline of a bubble, optionally with the tail merged
                    // into its bottom corner as part of the same closed path.
                    // Built for a right-hand bubble and mirrored through
                    // X() for incoming ones — mirroring reverses the winding,
                    // hence the flipped arc sweep flag.
                    function bubblePath(w0, h0, r0, hasTail, right) {
                        // Inset by half the rim stroke. The stroke is centred
                        // on the outline, so an outline spanning y ∈ [0, h]
                        // puts fill on rows 0..h-1 and leaves the stroke's
                        // outer half alone on row h — over the background,
                        // where it reads as a pale grey line under every
                        // bubble. The top edge escaped it only because its
                        // stroke fell on row -1, outside the item. Building
                        // the outline half a pixel in makes all four edges
                        // land inside the fill, and symmetric.
                        const inset = 0.5
                        const w = w0 - inset * 2
                        const h = h0 - inset * 2
                        // A single-line bubble is shorter than twice the
                        // corner radius. Unclamped, the side segment below
                        // runs *upwards*, the outline crosses itself, and the
                        // semi-transparent rim stroke paints over itself at
                        // each end — the pale specks that only ever appeared
                        // on one-line messages.
                        const r = Math.min(r0, h / 2)
                        const out = 6.5         // how far the tip reaches out
                        // Both axes shift by the inset; X also mirrors for
                        // incoming bubbles, which reverses the arc winding.
                        const X = x => (right ? x : (w - x)) + inset
                        const Y = y => y + inset
                        const s = right ? 1 : 0
                        const arc = (x, y) =>
                            ` A ${r} ${r} 0 0 ${s} ${X(x)} ${Y(y)}`
                        const L = (x, y) => ` L ${X(x)} ${Y(y)}`
                        const C = (x1, y1, x2, y2, x, y) =>
                            ` C ${X(x1)} ${Y(y1)}, ${X(x2)} ${Y(y2)}, ` +
                            `${X(x)} ${Y(y)}`

                        let p = `M ${X(r)} ${Y(0)}` + L(w - r, 0) + arc(w, r)
                        if (hasTail) {
                            // The tail replaces the bottom corner rather than
                            // hanging off a straight section of wall: the side
                            // flares directly into the point, then hooks back
                            // underneath. Keeping any straight run before the
                            // flare is what made the old one read as a blunt
                            // flap instead of a tail.
                            // ...but clamping r alone isn't enough here. At
                            // r == h/2 the top arc already ends at y == r,
                            // while the tail wants to start at h - r*1.05,
                            // which is *above* it — so the wall doubled back
                            // and the rim stroke overpainted itself again,
                            // leaving the speck on exactly the one-line
                            // bubbles that have a tail. Never start the flare
                            // higher than where the corner left off.
                            // The tip is deliberately not a corner: it ends a
                            // hair above the baseline and the return curve's
                            // first control point sits below and *outside* it,
                            // so the two curves meet through a small arc
                            // instead of a cusp. Pulling that control point
                            // back toward the wall is what sharpens it into a
                            // spike.
                            p += L(w, Math.max(r, h - r * 0.85))
                               + C(w, h - r * 0.28,
                                   w + out * 0.45, h - r * 0.30,
                                   w + out, h - 0.9)
                               + C(w + out * 0.55, h + 0.9,
                                   w - 4, h,
                                   w - r * 0.78, h)
                        } else {
                            p += L(w, h - r) + arc(w - r, h)
                        }
                        return p + L(r, h) + arc(0, h - r)
                                 + L(0, r) + arc(r, 0) + " Z"
                    }

                    // Fading is handled by a single gradient mask over the
                    // whole list (see msgList.layer below), so a tall bubble
                    // dissolves progressively from its top edge instead of
                    // dimming as one flat block.

                    // Time divider only where the conversation paused.
                    Item {
                        width: parent.width
                        height: divider === "" ? 0 : 30
                        visible: divider !== ""
                        Text {
                            anchors.centerIn: parent
                            text: divider
                            color: Theme.textDim
                            font.pixelSize: 11
                        }
                    }

                    // Sender label — group chats only, so you can tell who
                    // said what. Hidden in 1:1 threads where it's obvious.
                    Item {
                        width: parent.width
                        height: showSender ? 15 : 0
                        visible: showSender
                        readonly property bool showSender:
                            sender !== "" && !outgoing
                        Text {
                            // Indented past the bubble's corner curve so the
                            // name starts where the straight top edge does,
                            // rather than floating out over the round corner.
                            x: 16 + gutter + Theme.radiusBubble * 0.8
                            text: sender
                            color: Theme.textDim
                            font.pixelSize: 10
                        }
                    }

                    // The message being replied to, quoted above this one and
                    // on the same side of the thread. Without it a reply is
                    // visually identical to an ordinary message, which is
                    // indistinguishable from replying being broken.
                    Item {
                        width: msgList.width
                        height: msgDelegate.replyBody === ""
                                ? 0 : quoted.implicitHeight + 6
                        visible: height > 0

                        Row {
                            anchors.right: msgDelegate.outgoing
                                           ? parent.right : undefined
                            anchors.left: msgDelegate.outgoing
                                          ? undefined : parent.left
                            anchors.rightMargin: msgDelegate.sideMargin + 6
                            anchors.leftMargin: 16 + msgDelegate.gutter + 6
                            spacing: 5

                            // A short rule standing in for iOS's curved
                            // connector, marking the quote as attached to the
                            // bubble below rather than floating free.
                            Rectangle {
                                width: 2
                                height: quoted.implicitHeight
                                radius: 1
                                color: Theme.textDim
                                opacity: 0.45
                            }

                            Text {
                                id: quoted
                                width: Math.min(implicitWidth,
                                                msgList.width * 0.5)
                                text: msgDelegate.replyBody
                                elide: Text.ElideRight
                                maximumLineCount: 2
                                wrapMode: Text.Wrap
                                color: Theme.textDim
                                font.pixelSize: 11
                            }
                        }
                    }

                    // Previous versions of an edited message, revealed by the
                    // "Edited" link in the caption. They sit directly above
                    // the current text, oldest first, faded and italic — the
                    // point is to read them as superseded, not as messages
                    // in their own right.
                    Column {
                        width: parent.width
                        spacing: 2
                        visible: msgDelegate.showEdits
                                 && msgDelegate.edits.length > 0

                        Repeater {
                            model: msgDelegate.showEdits
                                   ? msgDelegate.edits : []

                            Item {
                                required property string modelData
                                width: msgList.width
                                height: oldText.implicitHeight + 4

                                Text {
                                    id: oldText
                                    // Aligned with the bubble it belongs to,
                                    // on the same side of the thread.
                                    anchors.right: msgDelegate.outgoing
                                                   ? parent.right : undefined
                                    anchors.left: msgDelegate.outgoing
                                                  ? undefined : parent.left
                                    anchors.rightMargin: msgDelegate.sideMargin
                                    anchors.leftMargin: 16 + msgDelegate.gutter
                                    width: Math.min(implicitWidth,
                                                    msgList.width * 0.62)
                                    text: parent.modelData
                                    wrapMode: Text.Wrap
                                    horizontalAlignment:
                                        msgDelegate.outgoing
                                        ? Text.AlignRight : Text.AlignLeft
                                    color: Theme.textDim
                                    opacity: 0.65
                                    font.pixelSize: 12
                                    font.italic: true
                                }
                            }
                        }
                    }

                    Item {
                        id: msgRow
                        width: parent.width
                        height: content.height + 4

                        // Right-click target, tracking whichever element is
                        // actually showing — a photo has no bubble, and an
                        // earlier version anchored to the bubble alone, which
                        // is why attachments couldn't be reacted to.
                        //
                        // Declared before the visuals so it sits beneath
                        // them; nothing above accepts right-clicks, so the
                        // events reach it either way.
                        MouseArea {
                            id: bubbleHit
                            anchors.fill: msgDelegate.isMedia ? mediaItem : bubble
                            // Generous: the bubble is sized tight to its text
                            // and a few px either way should still count.
                            anchors.margins: -4
                            // Not gated on the guid any more: Copy works
                            // without one, so a MAP message still has a menu
                            // worth opening.
                            enabled: msgDelegate.guid !== ""
                                     || msgDelegate.body !== ""
                            acceptedButtons: Qt.RightButton
                            onClicked: (mouse) => {
                                bubbleMenu.x = mouse.x
                                bubbleMenu.y = mouse.y - bubbleMenu.height - 4
                                bubbleMenu.open()
                            }
                        }

                        // Actions for one message. A plain Popup rather than
                        // a Menu: the tapback row has to be a row of
                        // individually-clickable emoji, and a MenuItem
                        // consumes the click before its contents ever see it.
                        Popup {
                            id: bubbleMenu
                            padding: 6
                            modal: true
                            dim: false
                            closePolicy: Popup.CloseOnEscape
                                         | Popup.CloseOnPressOutside

                            background: Rectangle {
                                color: Theme.dark ? Qt.rgba(0.17, 0.17, 0.19, 0.98)
                                                  : Qt.rgba(1, 1, 1, 0.98)
                                radius: 12
                                border.width: 1
                                border.color: Theme.separator
                            }

                            contentItem: Column {
                                spacing: 2

                                // The six classic tapbacks, in a row, the way
                                // iOS presents them.
                                Row {
                                    spacing: 2
                                    // Same rule as the verbs below: a tapback
                                    // names its target by guid, so a message
                                    // without one can't carry a reaction.
                                    visible: msgDelegate.guid !== ""
                                    Repeater {
                                        model: [
                                            { emoji: "❤️", kind: "Heart" },
                                            { emoji: "👍", kind: "Like" },
                                            { emoji: "👎", kind: "Dislike" },
                                            { emoji: "😂", kind: "Laugh" },
                                            { emoji: "‼️", kind: "Emphasize" },
                                            { emoji: "❓", kind: "Question" },
                                        ]
                                        Rectangle {
                                            required property var modelData
                                            width: 32; height: 32
                                            radius: 16
                                            color: tapHover.hovered
                                                   ? Qt.rgba(1, 1, 1, 0.12)
                                                   : "transparent"

                                            Text {
                                                anchors.centerIn: parent
                                                text: parent.modelData.emoji
                                                font.pixelSize: 17
                                            }

                                            HoverHandler { id: tapHover }
                                            TapHandler {
                                                onTapped: {
                                                    threadStore.react(
                                                        msgDelegate.guid,
                                                        parent.modelData.kind)
                                                    bubbleMenu.close()
                                                }
                                            }
                                        }
                                    }
                                }

                                Rectangle {
                                    width: parent.width
                                    height: 1
                                    color: Theme.separator
                                }

                                Repeater {
                                    model: [
                                        // Copy is the one action that names no
                                        // target on the wire, so unlike the
                                        // rest it works on a MAP message too.
                                        { label: "Copy", act: "copy",
                                          need: false, needGuid: false },
                                        { label: "Reply", act: "reply",
                                          need: false, needGuid: true },
                                        { label: "Edit", act: "edit",
                                          need: true, needGuid: true },
                                        { label: "Undo Send", act: "unsend",
                                          need: true, needGuid: true },
                                        // The one entry that isn't about this
                                        // message. It belongs here anyway:
                                        // right-clicking a bubble is the
                                        // obvious way to reach it, and this
                                        // menu covers the bubble the
                                        // background one would have handled.
                                        { label: msgArea.showTimes
                                                 ? "Hide Times" : "Show Times",
                                          act: "times", need: false,
                                          needGuid: false, always: true },
                                    ]
                                    Rectangle {
                                        required property var modelData
                                        // Edit and Undo Send only apply to
                                        // our own messages. The rest need a
                                        // guid to name their target —
                                        // offering Reply without one is what
                                        // made replying look broken: it left
                                        // the composer in plain mode and the
                                        // "reply" went out as an ordinary
                                        // message.
                                        readonly property bool allowed:
                                            modelData.always === true ||
                                            (modelData.needGuid
                                             ? msgDelegate.guid !== ""
                                             : msgDelegate.body !== "")
                                            && (!modelData.need
                                                || msgDelegate.outgoing)
                                        width: 190
                                        height: visible ? 30 : 0
                                        visible: allowed
                                        radius: 7
                                        color: rowHover.hovered
                                               ? Qt.rgba(1, 1, 1, 0.10)
                                               : "transparent"

                                        Text {
                                            anchors.verticalCenter: parent.verticalCenter
                                            anchors.left: parent.left
                                            anchors.leftMargin: 10
                                            text: parent.modelData.label
                                            color: Theme.text
                                            font.pixelSize: 12
                                        }

                                        HoverHandler { id: rowHover }
                                        TapHandler {
                                            onTapped: {
                                                var a = parent.modelData.act
                                                if (a === "times")
                                                    msgArea.showTimes =
                                                        !msgArea.showTimes
                                                else if (a === "copy")
                                                    // Selection wins when
                                                    // there is one — copying
                                                    // the whole message after
                                                    // the user deliberately
                                                    // highlighted part of it
                                                    // throws their work away.
                                                    threadStore.copyText(
                                                        msgText.selectedText
                                                        || msgDelegate.body)
                                                else if (a === "reply")
                                                    composer.beginReply(
                                                        msgDelegate.guid,
                                                        msgDelegate.body)
                                                else if (a === "edit")
                                                    composer.beginEdit(
                                                        msgDelegate.guid,
                                                        msgDelegate.body)
                                                else
                                                    threadStore.unsendMessage(
                                                        msgDelegate.guid)
                                                bubbleMenu.close()
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // ---- media: no bubble, stands on its own --------
                        Image {
                            id: mediaItem
                            visible: isMedia && status !== Image.Error
                            source: isMedia ? image : ""
                            anchors {
                                right: outgoing ? parent.right : undefined
                                left: outgoing ? undefined : parent.left
                                rightMargin: msgDelegate.sideMargin
                                leftMargin: 16 + gutter
                            }
                            y: 2
                            // Sized from the dimensions recorded at import
                            // rather than from the decoded image. Waiting for
                            // the decode meant every photo was 0px tall until
                            // it arrived and then shoved the list around,
                            // which is what made image threads scroll badly.
                            // 4:3 is the fallback for anything unmeasured.
                            readonly property real aspect:
                                (imageW > 0 && imageH > 0) ? (imageH / imageW)
                                                           : 0.75
                            width: Math.min(imageW > 0 ? imageW : 300,
                                            msgList.width * 0.55,
                                            isSticker ? 180 : 300)
                            height: Math.round(width * aspect)
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            // Cached: decoding is capped at sourceSize, so
                            // entries are small, and re-decoding on every
                            // recycle made images blink during scrolling.
                            cache: true
                            // iPhone photos are 12MP+; decoding at full
                            // resolution for a 300px view wastes memory.
                            sourceSize.width: 640
                            smooth: true
                            mipmap: true
                            // Honour the EXIF orientation tag. Phone photos
                            // are very often stored rotated with the true
                            // orientation only in metadata, so without this
                            // they display sideways.
                            autoTransform: true

                            // Photos get rounded corners; stickers stay bare
                            // and keep their transparency.
                            layer.enabled: isImage
                            layer.effect: OpacityMask { maskSource: imgMask }

                            TapHandler { onTapped: Qt.openUrlExternally(image) }
                        }

                        Rectangle {
                            id: imgMask
                            width: mediaItem.width
                            height: mediaItem.height
                            radius: 16
                            color: "white"
                            visible: false
                            layer.enabled: true
                            layer.smooth: true
                        }

                        // ---- text / file chip: normal bubble ------------
                        Rectangle {
                            id: bubble
                            visible: !isMedia

                            anchors {
                                right: outgoing ? parent.right : undefined
                                left: outgoing ? undefined : parent.left
                                rightMargin: msgDelegate.sideMargin
                                leftMargin: 16 + gutter
                            }
                            y: 2
                            readonly property bool isFile: fileLabel !== ""
                            readonly property int padH: isJumbo ? 4 : 28
                            readonly property int padV: isJumbo ? 2 : 16
                            width: isFile
                                   ? Math.min(240, msgList.width * 0.6)
                                   : Math.min(msgText.implicitWidth + padH,
                                              msgList.width * 0.68)
                            height: isFile ? 40 : msgText.implicitHeight + padV
                            radius: Theme.radiusBubble
                            // Body and tail are one filled path (see below),
                            // so this Rectangle is only geometry now. Painting
                            // them separately showed a seam: the bubbles are
                            // translucent over KWin's blur, so any overlap
                            // doubled the alpha and read as a stuck-on piece.
                            color: "transparent"

                            Shape {
                                anchors.fill: parent
                                visible: !isJumbo
                                // Curve renderer antialiases the tail's
                                // curves without the MSAA layer a Canvas or
                                // layer.samples would have cost per bubble.
                                preferredRendererType: Shape.CurveRenderer

                                ShapePath {
                                    fillColor: outgoing ? "transparent"
                                                        : Theme.bubbleIn
                                    fillGradient: outgoing ? outGradient : null
                                    strokeColor: Theme.bubbleRim
                                    strokeWidth: 1
                                    PathSvg {
                                        path: msgDelegate.bubblePath(
                                            bubble.width, bubble.height,
                                            Theme.radiusBubble,
                                            tail && !isMedia, outgoing)
                                    }
                                }

                                Shapes.LinearGradient {
                                    id: outGradient
                                    x1: 0; y1: 0
                                    x2: 0; y2: bubble.height
                                    GradientStop { position: 0.0; color: Theme.bubbleOutTop }
                                    GradientStop { position: 1.0; color: Theme.bubbleOutBot }
                                }
                            }

                            Row {
                                visible: bubble.isFile
                                anchors.centerIn: parent
                                spacing: 7
                                Icon {
                                    name: "paperclip"
                                    size: 14
                                    color: outgoing ? "white" : Theme.text
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Text {
                                    text: fileLabel
                                    color: outgoing ? "white" : Theme.text
                                    font.pixelSize: 12
                                    elide: Text.ElideMiddle
                                    width: 170
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            // TextEdit rather than Text so the message can be
                            // selected and copied with the mouse — Text has no
                            // selection at all. Read-only and with no frame,
                            // so it still looks and measures like a label:
                            // implicitWidth/implicitHeight drive the bubble
                            // geometry above exactly as before.
                            TextEdit {
                                id: msgText
                                readOnly: true
                                selectByMouse: true
                                // The context menu is modal, so opening it
                                // takes focus and would otherwise clear the
                                // selection before Copy could read it.
                                persistentSelection: true
                                // Selecting needs focus, but the caret would
                                // then blink inside a message bubble.
                                cursorVisible: false
                                // The default highlight is the accent blue,
                                // which on an outgoing bubble is the bubble's
                                // own colour — selected text there would be
                                // invisible.
                                selectionColor: outgoing
                                                ? Qt.rgba(1, 1, 1, 0.35)
                                                : Theme.accent
                                selectedTextColor: "white"
                                visible: !bubble.isFile
                                anchors {
                                    left: parent.left; right: parent.right
                                    verticalCenter: parent.verticalCenter
                                    leftMargin: bubble.padH / 2
                                    rightMargin: bubble.padH / 2
                                }
                                horizontalAlignment: isJumbo && outgoing
                                                     ? Text.AlignRight
                                                     : Text.AlignLeft
                                // Rich text only when there's emoji to
                                // resize — it's a heavier layout path, and
                                // most messages don't need it.
                                textFormat: richBody === "" ? Text.PlainText
                                                            : Text.RichText
                                text: mediaOnly
                                      ? "📎 Attachment — not sent over Bluetooth"
                                      : (richBody === "" ? body : richBody)
                                font.italic: mediaOnly
                                wrapMode: Text.Wrap
                                font.pixelSize: Math.round(13 * Theme.fontScale)
                                color: mediaOnly
                                       ? (outgoing ? Qt.rgba(1, 1, 1, 0.75)
                                                   : Theme.textDim)
                                       : (outgoing ? "white" : Theme.bubbleInText)
                            }

                            TapHandler {
                                enabled: bubble.isFile && image !== ""
                                onTapped: Qt.openUrlExternally(image)
                            }
                        }

                        // Sender avatar — drawn once per run, beside the
                        // last message in it.
                        Avatar {
                            visible: senderAvatar !== "" || senderInitials !== ""
                            size: 30
                            source: senderAvatar
                            initials: senderInitials || "?"
                            x: 12
                            anchors {
                                bottom: isMedia ? mediaItem.bottom : bubble.bottom
                            }
                        }

                        // Anchor for the row height + the tapback badge.
                        Item {
                            id: content
                            width: 1
                            height: isMedia ? mediaItem.height : bubble.height
                        }

                        // The Show Times gutter. Deliberately outside the
                        // slide the bubbles make: it stays pinned to the
                        // window edge so both sides of the conversation read
                        // down one column, which is the whole point of it.
                        Text {
                            id: timeLabel
                            text: msgDelegate.timeStamp
                            visible: opacity > 0
                            opacity: msgArea.showTimes ? 1 : 0
                            Behavior on opacity {
                                NumberAnimation {
                                    duration: Theme.animBase
                                    easing.type: Easing.OutCubic
                                }
                            }
                            color: Theme.textDim
                            font.pixelSize: 11
                            horizontalAlignment: Text.AlignRight
                            // Deliberately unsized: a fixed width narrower
                            // than "12:24 PM" does not elide it, it paints
                            // straight through the edge it was meant to
                            // respect. The gutter is sized for the text
                            // instead, not the other way round.
                            anchors {
                                right: parent.right
                                rightMargin: 18
                                verticalCenter: msgDelegate.isMedia
                                                ? mediaItem.verticalCenter
                                                : bubble.verticalCenter
                            }
                        }

                        // Tapback badge overlapping the corner nearest the
                        // centre of the view, as in Messages.app.
                        Image {
                            id: reactionBadge
                            source: reaction
                            visible: reaction !== ""
                            width: 30; height: 30
                            // Without sourceSize the SVG rasterizes at its
                            // intrinsic 117x115 and is then scaled down by
                            // the GPU, which is what made it look jagged.
                            sourceSize.width: 30 * Math.ceil(Screen.devicePixelRatio * 2)
                            sourceSize.height: 30 * Math.ceil(Screen.devicePixelRatio * 2)
                            smooth: true
                            mipmap: true
                            antialiasing: true
                            anchors {
                                verticalCenter: isMedia ? mediaItem.top : bubble.top
                                verticalCenterOffset: 6
                                left: outgoing ? (isMedia ? mediaItem.left : bubble.left)
                                               : undefined
                                right: outgoing ? undefined
                                                : (isMedia ? mediaItem.right : bubble.right)
                                leftMargin: -10
                                rightMargin: -10
                            }

                            Text {
                                visible: reactionEmoji !== ""
                                text: reactionEmoji
                                font.pixelSize: 13
                                anchors {
                                    horizontalCenter: parent.horizontalCenter
                                    horizontalCenterOffset:
                                        ((outgoing ? 66 / 117 : 51 / 117) - 0.5) * parent.width
                                    verticalCenter: parent.verticalCenter
                                    verticalCenterOffset: (51 / 115 - 0.5) * parent.height
                                }
                            }
                        }
                    }

                    // Delivery state, right-aligned under the newest outgoing
                    // bubble. Small and grey: it should be findable when
                    // you're waiting on it and invisible when you're not.
                    Item {
                        id: captionLine
                        // "Edited" earns the line on its own, even with no
                        // receipt yet — an edit you made is worth confirming.
                        readonly property bool hasEdits:
                            msgDelegate.edits.length > 0
                        readonly property bool shown:
                            msgDelegate.deliveryState !== "" || hasEdits

                        width: msgList.width
                        // Animated, so the thread opens the space for a
                        // receipt over several frames instead of jumping by
                        // 14px in one. The view welds itself to the bottom
                        // while this runs, so what you see is the whole
                        // conversation easing up by the height of the line.
                        height: shown ? 14 : 0
                        Behavior on height {
                            enabled: !msgDelegate.freshlyAdded
                            NumberAnimation {
                                duration: Theme.animArrive
                                easing.type: Easing.OutCubic
                            }
                        }
                        // Not `shown`: it has to stay rendered while the
                        // height animates back to zero.
                        visible: height > 0

                        Row {
                            // Fades up into place when a receipt lands, the
                            // same motion a new bubble makes. The band itself
                            // takes its 14px immediately — animating the
                            // height would drip dozens of tiny content-height
                            // changes into the view's bottom-follow, which
                            // expects one. So the layout moves once, smoothly,
                            // under the eased scroll, and the text rises
                            // within the space that opened for it.
                            //
                            // Bindings, not a Transition: on a delegate built
                            // with the caption already there, both evaluate to
                            // their resting values and nothing animates —
                            // which is what scrolling old messages into view
                            // should do.
                            opacity: captionLine.shown ? 1 : 0
                            Behavior on opacity {
                                enabled: !msgDelegate.freshlyAdded
                                NumberAnimation {
                                    duration: Theme.animArrive
                                    easing.type: Easing.OutCubic
                                }
                            }
                            transform: Translate {
                                y: captionLine.shown ? 0 : 6
                                Behavior on y {
                                    enabled: !msgDelegate.freshlyAdded
                                    NumberAnimation {
                                        duration: Theme.animArrive
                                        easing.type: Easing.OutCubic
                                    }
                                }
                            }

                            anchors.right: parent.right
                            // Flush with the bubble's right wall — same
                            // margin the bubble itself uses, so the caption
                            // ends exactly where the bubble does. The tail
                            // overhangs slightly past this; that's fine, it's
                            // the wall the eye reads as the edge.
                            anchors.rightMargin: msgDelegate.sideMargin
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 3

                            Text {
                                visible: msgDelegate.deliveryState !== ""
                                text: msgDelegate.deliveryState === "read"
                                      ? "Read" : "Delivered"
                                color: Theme.textDim
                                font.pixelSize: 10
                                font.bold: msgDelegate.deliveryState === "read"
                            }

                            Text {
                                visible: msgDelegate.deliveryStamp !== ""
                                text: msgDelegate.deliveryStamp
                                color: Theme.textDim
                                font.pixelSize: 10
                            }

                            Text {
                                visible: captionLine.hasEdits
                                         && msgDelegate.deliveryState !== ""
                                text: "·"
                                color: Theme.textDim
                                font.pixelSize: 10
                            }

                            Text {
                                id: editedLink
                                visible: captionLine.hasEdits
                                text: "Edited"
                                // Blue and underlined on hover: the only
                                // part of a caption that does anything, so
                                // it has to look unlike the rest of it.
                                color: Theme.accent
                                font.pixelSize: 10
                                font.underline: editedHover.hovered

                                HoverHandler {
                                    id: editedHover
                                    cursorShape: Qt.PointingHandCursor
                                }
                                TapHandler {
                                    onTapped: msgDelegate.showEdits =
                                              !msgDelegate.showEdits
                                }
                            }
                        }
                    }
                }
            }


                // Contact photo + name, centred over the conversation.
                Column {
                    z: 2
                    visible: msgArea.hasThread
                    anchors {
                        top: parent.top
                        topMargin: 8
                        horizontalCenter: parent.horizontalCenter
                    }
                    spacing: 4

                    Avatar {
                        anchors.horizontalCenter: parent.horizontalCenter
                        size: 36
                        source: threadStore.peerAvatar
                        initials: threadStore.peerInitials
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: threadStore.peerName
                        color: Theme.text
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }
                }
            }

            // Context banner: what the next Enter will do, when it isn't
            // simply "send a new message". Without this, reply and edit mode
            // are invisible states and the next keystroke is a surprise.
            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: 12
                Layout.rightMargin: 12
                Layout.preferredHeight: composer.pendingGuid === "" ? 0 : 28
                visible: Layout.preferredHeight > 0
                radius: 8
                color: Qt.rgba(1, 1, 1, 0.06)

                Behavior on Layout.preferredHeight {
                    NumberAnimation { duration: Theme.animFast }
                }

                Row {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    anchors.rightMargin: 6
                    spacing: 8

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: composer.editing ? "Editing" : "Replying to"
                        color: Theme.accent
                        font.pixelSize: 11
                        font.bold: true
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - 120
                        text: composer.pendingBody
                        color: Theme.textDim
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                }

                // Escape also cancels; this is for the mouse.
                Icon {
                    anchors.right: parent.right
                    anchors.rightMargin: 10
                    anchors.verticalCenter: parent.verticalCenter
                    name: "x"
                    size: 12
                    color: Theme.textDim
                    MouseArea {
                        anchors.fill: parent
                        anchors.margins: -6
                        cursorShape: Qt.PointingHandCursor
                        onClicked: composer.cancelPending()
                    }
                }
            }

            // Pill compose field, no send button — Enter sends, Shift+Enter
            // starts a new line.
            Rectangle {
                id: composer
                Layout.fillWidth: true
                Layout.margins: 12
                // Grows with the text and then stops, so a long message
                // scrolls inside the pill rather than eating the
                // conversation. 38 keeps the single-line pill exactly as it
                // was.
                readonly property int maxHeight: 140
                // Same measurement the field uses, so the pill and its
                // contents can never disagree about how tall one line is.
                Layout.preferredHeight: Math.min(
                    maxHeight, Math.max(38, composeFlick.height + 12))
                Behavior on Layout.preferredHeight {
                    NumberAnimation {
                        duration: Theme.animFast
                        easing.type: Easing.OutCubic
                    }
                }
                visible: msgArea.hasThread

                // Non-empty while composing a reply or an edit. `editing`
                // distinguishes the two, since both hold a guid.
                property string pendingGuid: ""
                property string pendingBody: ""
                property bool editing: false

                // Both refuse an empty guid rather than quietly dropping into
                // plain-send mode. The menu already hides these, but a stale
                // delegate could still get here, and silently sending an
                // unthreaded message is the worst available failure: it looks
                // like nothing happened until it turns up as a normal text.
                function beginReply(guid, body) {
                    if (guid === "")
                        return
                    pendingGuid = guid
                    pendingBody = body
                    editing = false
                    composeField.forceActiveFocus()
                }

                function beginEdit(guid, body) {
                    if (guid === "")
                        return
                    pendingGuid = guid
                    pendingBody = body
                    editing = true
                    // Edit starts from the existing text — you're amending
                    // it, not retyping it.
                    composeField.text = body
                    composeField.forceActiveFocus()
                }

                function cancelPending() {
                    if (editing)
                        composeField.text = ""
                    pendingGuid = ""
                    pendingBody = ""
                    editing = false
                }

                // Dispatch on whichever mode is active.
                function submit(text) {
                    if (text.trim() === "")
                        return
                    if (pendingGuid !== "" && editing)
                        threadStore.editMessage(pendingGuid, text)
                    else if (pendingGuid !== "")
                        threadStore.replyTo(pendingGuid, text)
                    else
                        threadStore.send(text)
                    pendingGuid = ""
                    pendingBody = ""
                    editing = false
                }
                // Pill while it's one line; once it grows, cap the corner so
                // it becomes a rounded box rather than a lozenge.
                radius: Math.min(height / 2, 19)
                color: Qt.rgba(1, 1, 1, 0.07)
                border.width: composeField.activeFocus ? 1.5 : 1
                border.color: composeField.activeFocus
                              ? Theme.accent : Theme.separator

                Behavior on border.color { ColorAnimation { duration: Theme.animFast } }

                // The TextArea lives inside a Flickable via the attached
                // `TextArea.flickable`, which is what keeps the caret in view
                // once the message outgrows `maxHeight`. A bare TextArea does
                // not scroll, so past the cap you would be typing into text
                // you cannot see.
                FontMetrics {
                    id: composeMetrics
                    font: composeField.font
                }

                // The placeholder. A sibling of the field rather than a child
                // of it, so it takes no part in the text layout at all — it
                // simply sits where the first line of text will sit, computed
                // from the same padding the field uses.
                Text {
                    id: composePlaceholder
                    visible: composeField.length === 0
                    text: "Message"
                    color: Theme.textDim
                    font: composeField.font
                    elide: Text.ElideRight
                    x: composeFlick.x + composeField.leftPadding
                    y: composeFlick.y + composeField.topPadding
                    width: composeFlick.width - composeField.leftPadding
                           - composeField.rightPadding
                }

                Flickable {
                    id: composeFlick
                    anchors.left: parent.left
                    anchors.right: parent.right
                    // Inset from the pill's rounded edge, not from its bounding
                    // box — at radius 19 the border curves in, so a small
                    // margin puts the caret visually tight against the stroke.
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    // Centred and sized to the text, not filling the pill.
                    // Filling it pinned the first line to the top, so a
                    // one-line message — and the placeholder — sat high in
                    // the pill instead of on its centre line.
                    anchors.verticalCenter: parent.verticalCenter
                    // Measured from the font and the line count, not from
                    // `composeField.implicitHeight`. The implicit height is a
                    // layout result, and on the very first frame — before the
                    // TextArea has been given its width and laid its
                    // placeholder out — it is wrong, so the field opened
                    // off-centre and only corrected once a keystroke forced a
                    // relayout. Line count and font metrics are known before
                    // any of that happens.
                    height: Math.min(composer.maxHeight - 12,
                                     Math.ceil(Math.max(
                                         composeField.contentHeight,
                                         composeMetrics.height))
                                     + composeField.topPadding
                                     + composeField.bottomPadding)
                    // Rounded up, because the two measurements disagree by
                    // half a pixel: font metrics said 15.5 where the laid-out
                    // line is 16. That left the view half a pixel shorter
                    // than its content, so it could scroll — and a caret on a
                    // half-pixel boundary is exactly the smeared, misplaced
                    // caret this was.
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    // While the field is empty the attached TextArea has not
                    // published a content height yet, and the Flickable's own
                    // is -1. Its bounds are then meaningless and contentY
                    // drifted to -6, drawing the caret six pixels below the
                    // line the text will actually occupy — the offset that
                    // corrected itself as soon as a keystroke or a second
                    // click forced the layout. Stating it keeps the bounds
                    // sane from the first frame.
                    contentHeight: Math.max(composeField.implicitHeight, height)

                    TextArea.flickable: TextArea {
                    id: composeField
                    // Deliberately not focused on startup. Taking focus put a
                    // caret in the field before its layout had settled, so the
                    // caret sat off its line until a click forced a relayout —
                    // the same first-frame problem the placeholder had, and it
                    // cannot be positioned by hand the way the placeholder is.
                    // Clicking the field, or Message → Compose, still focuses
                    // it (see focusComposer).
                    background: null
                    color: Theme.text
                    // Drawn by hand below instead. The built-in placeholder is
                    // a separate Text laid out against the control's height,
                    // and inside a Flickable that height is not settled on the
                    // first frame — so the prompt rendered off its line and
                    // only corrected when focus forced a relayout. Ours is
                    // positioned from the padding, which is a constant.
                    placeholderText: ""
                    font.pixelSize: 13
                    // Wrap rather than scroll sideways; Shift+Enter adds the
                    // explicit breaks.
                    wrapMode: TextArea.Wrap
                    topPadding: 6
                    bottomPadding: 6
                    leftPadding: 0
                    rightPadding: 0

                    // ---- :emoji autocomplete ---------------------------
                    property int tokenStart: -1
                    property var suggestions: []
                    property int highlighted: 0
                    readonly property bool popupOpen:
                        tokenStart >= 0 && suggestions.length > 0

                    function refreshSuggestions() {
                        var tok = emoji.tokenAt(text, cursorPosition)
                        if (tok.start < 0) {
                            tokenStart = -1
                            suggestions = []
                            return
                        }
                        var hits = emoji.search(tok.query)
                        tokenStart = hits.length > 0 ? tok.start : -1
                        suggestions = hits
                        highlighted = 0
                    }

                    function applySuggestion(index) {
                        if (!popupOpen)
                            return
                        var pick = suggestions[Math.max(0, Math.min(
                            index, suggestions.length - 1))]
                        var before = text.slice(0, tokenStart)
                        var after = text.slice(cursorPosition)
                        var insert = pick.emoji + " "
                        text = before + insert + after
                        cursorPosition = before.length + insert.length
                        tokenStart = -1
                        suggestions = []
                    }

                    onTextChanged: {
                        refreshSuggestions()
                        // Tell the other side we're composing, the way
                        // iMessage does. Sent on the first keystroke of a
                        // burst rather than every one — the indicator has
                        // its own timeout on their end, so repeating adds
                        // nothing.
                        if (text !== "") {
                            if (!typingStop.running)
                                threadStore.setTyping(true)
                            typingStop.restart()
                        }
                    }
                    onCursorPositionChanged: refreshSuggestions()

                    Timer {
                        id: typingStop
                        interval: 5000
                        onTriggered: threadStore.setTyping(false)
                    }

                    Keys.onPressed: (event) => {
                        var isReturn = event.key === Qt.Key_Return
                                       || event.key === Qt.Key_Enter
                        if (!popupOpen) {
                            // Back out of reply/edit mode. Only reachable
                            // once the emoji popup has had its turn, so
                            // Escape unwinds one layer at a time.
                            if (event.key === Qt.Key_Escape
                                    && composer.pendingGuid !== "") {
                                composer.cancelPending()
                                event.accepted = true
                                return
                            }
                            // Shift+Enter is a line break: left unaccepted so
                            // TextArea inserts it itself. Plain Enter sends —
                            // handled here rather than through `onAccepted`,
                            // which a TextArea does not have.
                            if (isReturn && !(event.modifiers & Qt.ShiftModifier)) {
                                composer.submit(text)
                                text = ""
                                typingStop.stop()
                                threadStore.setTyping(false)
                                event.accepted = true
                            }
                            return
                        }
                        // Tab takes the highlighted entry (the first, until
                        // arrows move it) — the Discord/Slack muscle memory.
                        if (event.key === Qt.Key_Tab) {
                            applySuggestion(highlighted)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Down) {
                            highlighted = Math.min(highlighted + 1,
                                                   suggestions.length - 1)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Up) {
                            highlighted = Math.max(highlighted - 1, 0)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Escape) {
                            tokenStart = -1
                            suggestions = []
                            // Emoji popup first, reply/edit mode second:
                            // Escape should back out one layer at a time.
                            event.accepted = true
                        } else if (isReturn) {
                            // Complete rather than sending a half-typed
                            // ":smi" — matches how Discord behaves.
                            applySuggestion(highlighted)
                            event.accepted = true
                        }
                    }

                    }
                }

                // ---- suggestion popup, floating above the composer ----
                Popup {
                    id: emojiPopup
                    visible: composeField.popupOpen
                    y: -height - 8
                    x: 6
                    width: 290
                    padding: 5
                    closePolicy: Popup.NoAutoClose

                    background: Rectangle {
                        color: Theme.dark ? Qt.rgba(0.16, 0.16, 0.18, 0.97)
                                          : Qt.rgba(1, 1, 1, 0.97)
                        radius: 12
                        border.width: 1
                        border.color: Theme.separator
                    }

                    enter: Transition {
                        NumberAnimation { property: "opacity"; from: 0; to: 1
                                          duration: Theme.animFast }
                    }

                    contentItem: Column {
                        spacing: 1
                        Repeater {
                            model: composeField.suggestions
                            delegate: Rectangle {
                                required property int index
                                required property var modelData
                                width: emojiPopup.width - 10
                                height: 28
                                radius: 7
                                color: index === composeField.highlighted
                                       ? Theme.accent
                                       : (hov.hovered ? Qt.rgba(1, 1, 1, 0.08)
                                                      : "transparent")

                                HoverHandler {
                                    id: hov
                                    // Hovering moves the selection, so Tab
                                    // and a click always agree.
                                    onHoveredChanged: if (hovered)
                                        composeField.highlighted = index
                                }

                                Row {
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.left: parent.left
                                    anchors.leftMargin: 9
                                    spacing: 9
                                    Text {
                                        text: modelData.emoji
                                        font.pixelSize: 15
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                    Text {
                                        text: ":" + modelData.code
                                        color: index === composeField.highlighted
                                               ? "white" : Theme.textDim
                                        font.pixelSize: 12
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                }

                                TapHandler {
                                    onTapped: {
                                        composeField.applySuggestion(index)
                                        composeField.forceActiveFocus()
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
