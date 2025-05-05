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

PUBLIC_KEY = '-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4PnZAVUd2ocV4qgLrJ6mKd19/BANI5OScCSHsAbqAysFnpxABQMkJsrNbHvt5IAVyTnIxyTCRDYsudcPhNdUJ8RjiXR5MPXvBwu5slSP1jwxaBBP/e4EXi5I7i7ZyD/C9b2H7ruRUg/Hn2pUXAUuKihFDyOblJBSk5MRVTwfUJysimh5/szVFt11eNzbRf0SqW130eft97JiL7MpfhOSA2dpDgnV/AuWv1vEjQZObZn6RTBI648L/Sgkb+dYEtuE/q0frEuiT61Gbyw1MjrKW1LXkFldL7T30PJs7bds3hlH/Xw5u6ySqwRJDqv4135bC3lfmoX+IzWsXZ487s4+0QIDAQAB\n-----END PUBLIC KEY-----'


class Service(cf.CentrifugoProxyServicer):

    async def Connect(
        self,
        request: pb.ConnectRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.ConnectResponse:
        print(request, flush=True)
        data = json.loads(request.data.decode())
        token_data =jwt.decode(data['token'], PUBLIC_KEY, algorithms=['RS256'], audience='account', verify=True)
        user_data = {
            'id': token_data['sub'],
            'username': token_data['preferred_username'],
            'given_name': token_data['given_name'],
            'family_name': token_data['family_name'],
            'email': token_data['email'],
        }
        async with engine.connect() as connection:
            querier = ws_requests.AsyncQuerier(connection)
            user = await querier.get_user_by_id(id=user_data['id'])
            if not user:
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
                user=user_data['id'],
            ),
        )

    @staticmethod
    async def serve() -> None:
        server = grpc.aio.server()
        cf.add_CentrifugoProxyServicer_to_server(Service(), server)
        server.add_insecure_port('[::]:2121')
        await server.start()
        await server.wait_for_termination()



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
            await connection.commit()
        return pb.SubscribeResponse()

    

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