from os import path as op
import tornado
import tornado.options
import tornado.web
import tornado.httpserver
import tornadio2
import tornadio2.router
import tornadio2.server
import tornadio2.conn
import redis
import json
import re
import collections
import threading
import urllib
# Globals
redis_client = None
redis_pubsub = None
clients = collections.defaultdict(set)


class PubSubThread(threading.Thread):
    def run(self):
        redis_pubsub.psubscribe("*")
        for message in redis_pubsub.listen():
            data = json.loads(message['data'])
            if message['type'] == "pmessage":

                # Server-wide notice from admin
                if message['channel'] == "server-notice":
                    for chat_id in clients.keys():
                        for client in clients[chat_id]:
                            client.send_message(
                                type = "alert-message warning",
                                user_id = "Server",
                                body = data['body']
                            )

                # Normal message to a specific chatroom
                else:
                    for client in clients[message['channel']]:
                        client.send_message(
                            type = data['type'],
                            user_id = data['user_id'],
                            body = data['body']
                        )

class RequestHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        return self.get_cookie("user_id")

class IndexHandler(RequestHandler):
    """chatroom page login page"""
    def get(self):
        data = dict(
            num_users = redis_client.get("num_users"),
            num_chats = format(redis_client.zcard("chats"), ",d"),
            popular_chats = redis_client.zrevrange("chats", 0, 9, True),
            errors = [],
        )
        self.render("index.html", **data)

class ChatHandler(RequestHandler):
    """Regular HTTP handler to serve the chatroom page"""
    @tornado.web.authenticated
    def get(self, chat_id):
        data = dict(
            num_users = redis_client.get("num_users"),
            num_chats = format(redis_client.zcard("chats"), ",d"),
            popular_chats = redis_client.zrevrange("chats", 0, 9, True),
        )

        # Check for valid user_id
#        if not re.match("^[A-Za-z0-9_]{3,16}$", self.current_user):
#            data['errors'] = ["Invalid username."]
#            self.render("index.html", **data)
#            return

        #Check user_id isn't in use in chat_id
        for user_id in redis_client.lrange(chat_id, 0, -1):
            if user_id == self.current_user:
#                data['errors'] = [
#                    "Username %s already in chatroom %s."\
#                    % (self.current_user, chat_id)
#                ]
#                self.render("index.html", **data)
#                return
                redis_client.lrem(chat_id, self.current_user)
                redis_client.decr("num_users")
                if (redis_client.zincrby("chats", chat_id, -1) < 1):
                    redis_client.zrem("chats", chat_id)
#
        self.render("chat.html")

class ChatConnection(tornadio2.conn.SocketConnection):
    # Class level variable
#    participants = set()

    def on_open(self, info):
#        self.send("Welcome from the server.")
#        self.participants.add(self)
        self.current_user = info.get_cookie("user_id").value
        self.chat_id = chat_id = info.get_cookie("chat_id").value
        redis_client.incr("num_users")
        redis_client.zincrby("chats", chat_id, 1)
        redis_client.rpush(chat_id, self.current_user)
        clients[chat_id].add(self)
        data = dict(
            type = "alert-message success",
            user_id = urllib.unquote(self.current_user),
            body = "has entered %s" % (chat_id),
        )
        redis_client.publish(chat_id, json.dumps(data))
        self.list_users()

    def on_message(self, message):
        # Pong message back
        if message == "/users":
            self.list_users()
        else:
            data = dict(
                type = "chat",
                user_id = urllib.unquote(self.current_user),
                body = tornado.escape.linkify(message),
            )
            redis_client.publish(self.chat_id, json.dumps(data))


    def on_close(self):
#        self.participants.remove(self)
        print "in on_close"
        try:
            clients[self.chat_id].remove(self)
            data = dict(
                type = "alert-message error",
                user_id = urllib.unquote(self.current_user),
                body = "has left %s" % (self.chat_id),
            )
            redis_client.publish(self.chat_id, json.dumps(data))
            redis_client.lrem(self.chat_id, self.current_user)
            redis_client.decr("num_users")
            if (redis_client.zincrby("chats", self.chat_id, -1) < 1):
                redis_client.zrem("chats", self.chat_id)
        except:
            pass

    def send_message(self, body, type="chat", user_id=False):
        if not user_id:
            user_id = self.current_user
        message = dict(
            type = type,
            user_id = user_id,
            body = body,
        )
        self.send(json.dumps(message))

    def list_users(self):
        users = redis_client.lrange(self.chat_id, 0, -1)
        users.remove(self.current_user)
        if len(users) > 0:
            users.sort()
            body = "Currently chatting with " + ", ".join(map(urllib.unquote,users)),
        else:
            body = "Waiting for someone to chat with.",
        self.send_message(body, type="alert-message block-message info")

def main():
    tornado.options.parse_command_line()
    import logging
    logging.getLogger().setLevel(logging.ERROR)

    web_port = 8001
    redis_host = "localhost"

    global redis_client
    global redis_pubsub
    redis_client = redis.Redis(redis_host)
    redis_pubsub = redis_client.pubsub()

    pubsub = PubSubThread()
    pubsub.daemon = True

    # Create tornadio server
    ChatRouter = tornadio2.router.TornadioRouter(ChatConnection)

    # Create socket application
    sock_app = tornado.web.Application(
        ChatRouter.urls,
        #    flash_policy_port = 843,
        #    flash_policy_file = op.join(op.normpath(op.dirname(__file__)), 'flashpolicy.xml'),
        socket_io_port = 8002
    )
    # Create tornadio server on port 8002, but don't start it yet
    tornadio2.server.SocketServer(sock_app, auto_start=False)


    # Create HTTP application
    http_app = tornado.web.Application(
        handlers = [(r"/", IndexHandler),
                    (r"/([A-Za-z0-9_]{1,32})", ChatHandler)],
        template_path=op.join(op.dirname(__file__), "templates"),
        static_path=op.join(op.dirname(__file__), "static"),
        login_url = "/",
        debug=True
    )

    # Create http server on port 8001
    http_server = tornado.httpserver.HTTPServer(http_app)
    http_server.listen(web_port)

    # Start both servers
    # Allow Ctrl+C to exit cleanly
    try:
        pubsub.start()
        tornado.ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        exit()

if __name__ == "__main__":
    main()