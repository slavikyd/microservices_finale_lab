import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import grpc
import jwt
import uvicorn
from fastapi import FastAPI

import proto.main_pb2 as pb
import proto.main_pb2_grpc as cf
from db import ws_requests
from db.postgres import engine

import os
import datetime
import httpx

key_begin = '-----BEGIN PUBLIC KEY-----\n'
key_end = '\n-----END PUBLIC KEY-----'
client_key = ''
backend_key = {
    'token': '',
    'expire_at': datetime.datetime.now(),
}

class Service(cf.CentrifugoProxyServicer):

    async def Connect(
        self,
        request: pb.ConnectRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.ConnectResponse:
        data = json.loads(request.data.decode())
        global client_key
        if not client_key:
            async with httpx.AsyncClient() as client:
                config_data = (await client.get(f"http://keycloak:8080/realms/master")).json()
            client_key = config_data['public_key']
            if not client_key.startswith(key_begin):
                client_key = f'{key_begin}{client_key}'
            if not client_key.endswith(key_end):
                client_key = f'{client_key}{key_end}'
        token_data =jwt.decode(data['token'], client_key, algorithms=['RS256'], audience='account', verify=True)
        user_id = token_data['sub']
        async with engine.connect() as connection:
            querier = ws_requests.AsyncQuerier(connection)
            user = await querier.get_user_by_id(id=user_id)
            if not user:
                if backend_key['expire_at'] < datetime.datetime.now():
                    async with httpx.AsyncClient() as client:
                        backend_token = await client.post(
                            f"http://keycloak:8080/realms/{os.getenv('KEYCLOAK_REALM')}/protocol/openid-connect/token",
                            headers={
                                'Content-Type': 'application/x-www-form-urlencoded',
                            },
                            data={
                                'client_id': 'backend',
                                'grant_type': 'client_credentials',
                                'client_secret': {os.getenv('KC_BACKEND_SECRET')},
                            },
                        )
                        backend_token = backend_token.json()
                    backend_key['token'] = backend_token['access_token']
                    backend_key['expire_at'] = datetime.datetime.now() + datetime.timedelta(seconds=backend_token['expires_in'])
                async with httpx.AsyncClient() as client:
                    kc_user_data = (await client.get(
                        f"http://keycloak:8080/admin/realms/{os.getenv('KEYCLOAK_REALM')}/users/{user_id}",
                        headers={
                            'Authorization': f'Bearer {backend_key["token"]}'
                        }
                    )).json()
                user_data = {
                    'id': kc_user_data['id'],
                    'username': kc_user_data['username'],
                    'given_name': kc_user_data['firstName'],
                    'family_name': kc_user_data['lastName'],
                    'email': kc_user_data['email'],
                }
                await querier.create_user(
                    id=user_data['id'],
                    username=user_data['username'],
                    given_name=user_data['given_name'],
                    family_name=user_data['family_name'],
                    enabled=True,
                )
            await connection.commit()
        return pb.ConnectResponse(
            result=pb.ConnectResult(
                user=user_id,
            ),
        )

    async def Subscribe(
        self,
        request: pb.SubscribeRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.SubscribeResponse:
        async with engine.connect() as connection:
            querier = ws_requests.AsyncQuerier(connection)
            await querier.subscribe_user_to_channel(
                user_id=request.user,
                channel=request.channel,
                can_publish=True,
            )
            # if ws_requests.AsyncQuerier.user_can_subscribe(request.user):
            #     pass
            # else:
            #     print('User cannot subscribe. Not allowed', flush=True)
            #     return 401 # TODO: some proper return

            await connection.commit()
        return pb.SubscribeResponse()

    async def Publish(
        self,
        request: pb.PublishRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.PublishResponse:
        async with engine.connect() as connection:
            querier = ws_requests.AsyncQuerier(connection)
            can_publish = await querier.user_can_publish(
                user_id=request.user,
                channel=request.channel,
            )
        if can_publish:
            return pb.PublishResponse()
        return pb.PublishResponse(
            error=pb.Error(code=103),
        )

    async def RPC(
        self,
        request: pb.RPCRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.RPCResponse:
        if request.method == 'get_user_channels':
            async with engine.connect() as connection:
                querier = ws_requests.AsyncQuerier(connection)
                channels = [channel.channel async for channel in querier.chan_list_by_user_id(user_id=request.user)]
            return pb.RPCResponse(
                result=pb.RPCResult(
                    data=f'{{ "channels": {json.dumps(channels)} }}'.encode(),
                ),
            )
        return super().RPC(request, context)

    @staticmethod
    async def serve() -> None:
        server = grpc.aio.server()
        cf.add_CentrifugoProxyServicer_to_server(Service(), server)
        server.add_insecure_port('[::]:2121')
        await server.start()
        await server.wait_for_termination()



    

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    grpc_task = asyncio.create_task(Service.serve())
    yield
    grpc_task.cancel()


def create_app() -> FastAPI:
    return FastAPI(docs_url='/swagger', lifespan=lifespan)


if __name__ == '__main__':
    uvicorn.run(
        'main:create_app',
        factory=True,
        host='127.0.0.1',
        port=8001,  
        workers=1,
        access_log=False,
    )