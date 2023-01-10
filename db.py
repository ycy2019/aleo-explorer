import time

import asyncpg

from explorer.types import Message as ExplorerMessage
from node.types import *


class Database:

    def __init__(self, *, server: str, user: str, password: str, database: str, schema: str,
                 message_callback: callable):
        self.server = server
        self.user = user
        self.password = password
        self.database = database
        self.schema = schema
        self.message_callback = message_callback
        self.pool = None

    async def connect(self):
        try:
            self.pool = await asyncpg.create_pool(host=self.server, user=self.user, password=self.password,
                                                  database=self.database, server_settings={'search_path': self.schema},
                                                  min_size=1, max_size=4)
        except Exception as e:
            await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseConnectError, e))
            return
        await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseConnected, None))

    async def _save_block(self, block: Block):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                try:
                    block_db_id = await conn.fetchval(
                        "INSERT INTO block (height, block_hash, previous_hash, previous_state_root, transactions_root, "
                        "coinbase_accumulator_point, round, coinbase_target, proof_target, last_coinbase_target, "
                        "last_coinbase_timestamp, timestamp, signature) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
                        "RETURNING id",
                        block.header.metadata.height, str(block.block_hash), str(block.previous_hash),
                        str(block.header.previous_state_root), str(block.header.transactions_root),
                        str(block.header.coinbase_accumulator_point), block.header.metadata.round,
                        block.header.metadata.coinbase_target, block.header.metadata.proof_target,
                        block.header.metadata.last_coinbase_target, block.header.metadata.last_coinbase_timestamp,
                        block.header.metadata.timestamp, str(block.signature)
                    )

                    transaction: Transaction
                    for tx_index, transaction in enumerate(block.transactions):
                        match transaction.type:
                            case Transaction.Type.Deploy:
                                raise NotImplementedError
                            case Transaction.Type.Execute:
                                transaction: ExecuteTransaction
                                transaction_id = transaction.id
                                transaction_db_id = await conn.fetchval(
                                    "INSERT INTO transaction (block_id, transaction_id, type) VALUES ($1, $2, $3) RETURNING id",
                                    block_db_id, str(transaction_id), transaction.type.name
                                )
                                execute_transaction_db_id = await conn.fetchval(
                                    "INSERT INTO transaction_execute (transaction_id, global_state_root, inclusion_proof, index) "
                                    "VALUES ($1, $2, $3, $4) RETURNING id",
                                    transaction_db_id, str(transaction.execution.global_state_root),
                                    transaction.execution.inclusion_proof.dumps(), tx_index
                                )

                                transition: Transition
                                for transition in transaction.execution.transitions:
                                    transition_db_id = await conn.fetchval(
                                        "INSERT INTO transition (transition_id, transaction_execute_id, fee_id, program_id, "
                                        "function_name, proof, tpk, tcm, fee) "
                                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
                                        str(transition.id), execute_transaction_db_id, None, str(transition.program_id),
                                        str(transition.function_name), str(transition.proof), str(transition.tpk),
                                        str(transition.tcm), transition.fee
                                    )

                                    transition_input: TransitionInput
                                    for input_index, transition_input in enumerate(transition.inputs):
                                        transition_input_db_id = await conn.fetchval(
                                            "INSERT INTO transition_input (transition_id, type) VALUES ($1, $2) RETURNING id",
                                            transition_db_id, transition_input.type.name
                                        )
                                        match transition_input.type:
                                            case TransitionInput.Type.Private:
                                                transition_input: PrivateTransitionInput
                                                await conn.execute(
                                                    "INSERT INTO transition_input_private (transition_input_id, ciphertext_hash, ciphertext, index) "
                                                    "VALUES ($1, $2, $3, $4)",
                                                    transition_input_db_id, str(transition_input.ciphertext_hash),
                                                    transition_input.ciphertext.dumps(), input_index
                                                )
                                            case TransitionInput.Type.Record:
                                                transition_input: RecordTransitionInput
                                                await conn.execute(
                                                    "INSERT INTO transition_input_record (transition_input_id, serial_number, tag, index) "
                                                    "VALUES ($1, $2, $3, $4)",
                                                    transition_input_db_id, str(transition_input.serial_number),
                                                    str(transition_input.tag), input_index
                                                )
                                            case _:
                                                raise NotImplementedError

                                    transition_output: TransitionOutput
                                    for output_index, transition_output in enumerate(transition.outputs):
                                        transition_output_db_id = await conn.fetchval(
                                            "INSERT INTO transition_output (transition_id, type) VALUES ($1, $2) RETURNING id",
                                            transition_db_id, transition_output.type.name
                                        )
                                        match transition_output.type:
                                            case TransitionOutput.Type.Record:
                                                transition_output: RecordTransitionOutput
                                                await conn.execute(
                                                    "INSERT INTO transition_output_record (transition_output_id, commitment, checksum, record_ciphertext, index) "
                                                    "VALUES ($1, $2, $3, $4, $5)",
                                                    transition_output_db_id, str(transition_output.commitment),
                                                    str(transition_output.checksum), transition_output.record_ciphertext.dumps(), output_index
                                                )
                                            case _:
                                                raise NotImplementedError

                                    if transition.finalize.value is not None:
                                        raise NotImplementedError

                    if block.coinbase.value is not None:
                        coinbase_reward = block.get_coinbase_reward((await self.get_latest_block()).header.metadata.last_coinbase_timestamp)
                        await conn.execute(
                            "UPDATE block SET coinbase_reward = $1 WHERE id = $2",
                            coinbase_reward, block_db_id
                        )
                        partial_solutions = list(block.coinbase.value.partial_solutions)
                        solutions = []
                        partial_solutions = list(zip(partial_solutions,
                                                [partial_solution.commitment.to_target() for partial_solution in
                                                 partial_solutions]))
                        target_sum = sum(target for _, target in partial_solutions)
                        partial_solution: PartialSolution
                        for partial_solution, target in partial_solutions:
                            solutions.append((partial_solution, target, coinbase_reward * target // (2 * target_sum)))

                        coinbase_solution_db_id = await conn.fetchval(
                            "INSERT INTO coinbase_solution (block_id, proof_x, proof_y_positive, target_sum) "
                            "VALUES ($1, $2, $3, $4) RETURNING id",
                            block_db_id, str(block.coinbase.value.proof.w.x), block.coinbase.value.proof.w.flags, target_sum
                        )
                        current_total_credit = await conn.fetchval("SELECT total_credit FROM leaderboard_total")
                        if current_total_credit is None:
                            await conn.execute("INSERT INTO leaderboard_total (total_credit) VALUES (0)")
                            current_total_credit = 0
                        partial_solution: PartialSolution
                        for partial_solution, target, reward in solutions:
                            await conn.execute(
                                "INSERT INTO partial_solution (coinbase_solution_id, address, nonce, commitment, target, reward) "
                                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                                coinbase_solution_db_id, str(partial_solution.address), partial_solution.nonce,
                                str(partial_solution.commitment), partial_solution.commitment.to_target(), reward
                            )
                            if reward > 0:
                                await conn.execute(
                                    "INSERT INTO leaderboard (address, total_reward) VALUES ($1, $2) "
                                    "ON CONFLICT (address) DO UPDATE SET total_reward = leaderboard.total_reward + $2",
                                    str(partial_solution.address), reward
                                )
                                if block.header.metadata.height >= 130888 and block.header.metadata.timestamp < 1674777600 and current_total_credit < 37_500_000_000_000:
                                    await conn.execute(
                                        "UPDATE leaderboard SET total_incentive = leaderboard.total_incentive + $1 WHERE address = $2",
                                        reward, str(partial_solution.address)
                                    )
                        if block.header.metadata.height >= 130888 and block.header.metadata.timestamp < 1674777600 and current_total_credit < 37_500_000_000_000:
                            await conn.execute(
                                "UPDATE leaderboard_total SET total_credit = leaderboard_total.total_credit + $1",
                                sum(reward for _, _, reward in solutions)
                            )

                    await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseBlockAdded, block.header.metadata.height))
                except Exception as e:
                    await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                    breakpoint()
                    raise

    async def save_block(self, block: Block):
        await self._save_block(block)

    @staticmethod
    def _get_block_header(block: dict):
        return BlockHeader(
            previous_state_root=Field.loads(block["previous_state_root"]),
            transactions_root=Field.loads(block["transactions_root"]),
            coinbase_accumulator_point=Field.loads(block["coinbase_accumulator_point"]),
            metadata=BlockHeaderMetadata(
                network=u16(3),
                round_=u64(block["round"]),
                height=u32(block["height"]),
                coinbase_target=u64(block["coinbase_target"]),
                proof_target=u64(block["proof_target"]),
                last_coinbase_target=u64(block["last_coinbase_target"]),
                last_coinbase_timestamp=i64(block["last_coinbase_timestamp"]),
                timestamp=i64(block["timestamp"]),
            )
        )

    @staticmethod
    async def _get_full_block(block: dict, conn: asyncpg.Connection):
        transactions = await conn.fetch("SELECT * FROM transaction WHERE block_id = $1", block['id'])
        txs = []
        for transaction in transactions:
            match transaction["type"]:
                case Transaction.Type.Execute.name:
                    execute_transaction = await conn.fetchrow(
                        "SELECT * FROM transaction_execute WHERE transaction_id = $1", transaction["id"]
                    )
                    transitions = await conn.fetch(
                        "SELECT * FROM transition WHERE transaction_execute_id = $1",
                        execute_transaction["id"]
                    )
                    tss = []
                    for transition in transitions:
                        transition_inputs = await conn.fetch(
                            "SELECT * FROM transition_input WHERE transition_id = $1",
                            transition["id"]
                        )
                        tis = []
                        for transition_input in transition_inputs:
                            match transition_input["type"]:
                                case TransitionInput.Type.Private.name:
                                    transition_input_private = await conn.fetchrow(
                                        "SELECT * FROM transition_input_private WHERE transition_input_id = $1",
                                        transition_input["id"]
                                    )
                                    if transition_input_private is None:
                                        ciphertext = None
                                    else:
                                        ciphertext = Ciphertext.loads(transition_input_private["ciphertext"])
                                    tis.append((PrivateTransitionInput(
                                        ciphertext_hash=Field.loads(transition_input_private["ciphertext_hash"]),
                                        ciphertext=Option[Ciphertext](ciphertext)
                                    ), transition_input_private["index"]))
                                case TransitionInput.Type.Record.name:
                                    transition_input_record = await conn.fetchrow(
                                        "SELECT * FROM transition_input_record WHERE transition_input_id = $1",
                                        transition_input["id"]
                                    )
                                    tis.append((RecordTransitionInput(
                                        serial_number=Field.loads(transition_input_record["serial_number"]),
                                        tag=Field.loads(transition_input_record["tag"])
                                    ), transition_input_record["index"]))
                                case _:
                                    raise NotImplementedError
                        tis.sort(key=lambda x: x[1])
                        tis = [x[0] for x in tis]

                        transition_outputs = await conn.fetch(
                            "SELECT * FROM transition_output WHERE transition_id = $1",
                            transition["id"]
                        )
                        tos = []
                        for transition_output in transition_outputs:
                            match transition_output["type"]:
                                case TransitionOutput.Type.Record.name:
                                    transition_output_record = await conn.fetchrow(
                                        "SELECT * FROM transition_output_record WHERE transition_output_id = $1",
                                        transition_output["id"]
                                    )
                                    if transition_output_record["record_ciphertext"] is None:
                                        record_ciphertext = None
                                    else:
                                        record_ciphertext = Record[Ciphertext].loads(transition_output_record["record_ciphertext"])
                                    tos.append((RecordTransitionOutput(
                                        commitment=Field.loads(transition_output_record["commitment"]),
                                        checksum=Field.loads(transition_output_record["checksum"]),
                                        record_ciphertext=Option[Record[Ciphertext]](record_ciphertext)
                                    ), transition_output_record["index"]))
                                case _:
                                    raise NotImplementedError
                        tos.sort(key=lambda x: x[1])
                        tos = [x[0] for x in tos]
                        tss.append(Transition(
                            id_=TransitionID.loads(transition["transition_id"]),
                            program_id=ProgramID.loads(transition["program_id"]),
                            function_name=Identifier.loads(transition["function_name"]),
                            inputs=Vec[TransitionInput, u16](tis),
                            outputs=Vec[TransitionOutput, u16](tos),
                            # This is wrong
                            finalize=Option[Vec[Value, u16]](None),
                            proof=Proof.loads(transition["proof"]),
                            tpk=Group.loads(transition["tpk"]),
                            tcm=Field.loads(transition["tcm"]),
                            fee=i64(transition["fee"]),
                        ))
                    additional_fee = await conn.fetchrow(
                        "SELECT * FROM fee WHERE transaction_id = $1", transaction["id"]
                    )
                    if additional_fee is None:
                        fee = None
                    else:
                        raise NotImplementedError
                    if execute_transaction["inclusion_proof"] is None:
                        proof = None
                    else:
                        proof = Proof.loads(execute_transaction["inclusion_proof"])
                    txs.append(ExecuteTransaction(
                        id_=TransactionID.loads(transaction["transaction_id"]),
                        execution=Execution(
                            transitions=Vec[Transition, u16](tss),
                            global_state_root=StateRoot.loads(execute_transaction["global_state_root"]),
                            inclusion_proof=Option[Proof](proof),
                        ),
                        additional_fee=Option[Fee](fee),
                    ))
                case _:
                    raise NotImplementedError
        coinbase_solution = await conn.fetchrow("SELECT * FROM coinbase_solution WHERE block_id = $1", block["id"])
        if coinbase_solution is not None:
            partial_solutions = await conn.fetch(
                "SELECT * FROM partial_solution WHERE coinbase_solution_id = $1",
                coinbase_solution["id"]
            )
            pss = []
            for partial_solution in partial_solutions:
                pss.append(PartialSolution.load_json(dict(partial_solution)))
            coinbase_solution = CoinbaseSolution(
                partial_solutions=Vec[PartialSolution, u32](pss),
                proof=KZGProof(
                    w=G1Affine(
                        x=Fq(value=int(coinbase_solution["proof_x"])),
                        # This is very wrong
                        flags=False,
                    ),
                    random_v=Option[Field](None),
                )
            )
        else:
            coinbase_solution = None

        return Block(
            block_hash=BlockHash.loads(block['block_hash']),
            previous_hash=BlockHash.loads(block['previous_hash']),
            header=Database._get_block_header(block),
            transactions=Transactions(
                transactions=Vec[Transaction, u32](txs),
            ),
            coinbase=Option[CoinbaseSolution](coinbase_solution),
            signature=Signature.loads(block['signature']),
        )

    @staticmethod
    async def _get_full_block_range(start: int, end: int, conn: asyncpg.Connection):
        blocks = await conn.fetch(
            "SELECT * FROM block WHERE height <= $1 AND height > $2 ORDER BY height DESC",
            start, end
        )
        return [await Database._get_full_block(block, conn) for block in blocks]

    @staticmethod
    async def _get_fast_block(block: dict, conn: asyncpg.Connection) -> dict:
        transaction_count = await conn.fetchval(
            "SELECT COUNT(*) FROM transaction WHERE block_id = $1",
            block["id"]
        )
        partial_solution_count = await conn.fetchval(
            "SELECT COUNT(*) FROM partial_solution ps "
            "JOIN coinbase_solution cs on ps.coinbase_solution_id = cs.id "
            "WHERE cs.block_id = $1",
            block["id"]
        )
        return {
            **block,
            "transaction_count": transaction_count,
            "partial_solution_count": partial_solution_count,
        }

    @staticmethod
    async def _get_fast_block_range(start: int, end: int, conn: asyncpg.Connection):
        blocks = await conn.fetch(
            "SELECT * FROM block WHERE height <= $1 AND height > $2 ORDER BY height DESC",
            start, end
        )
        return [await Database._get_fast_block(block, conn) for block in blocks]


    async def get_latest_height(self):
        async with self.pool.acquire() as conn:
            try:
                result = await conn.fetchrow(
                    "SELECT height FROM block ORDER BY height DESC LIMIT 1")
                if result is None:
                    return None
                return result['height']
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_latest_block(self):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                block = await conn.fetchrow(
                    "SELECT * FROM block ORDER BY height DESC LIMIT 1")
                if block is None:
                    return None
                return await self._get_full_block(block, conn)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_by_height(self, height: u32):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                block = await conn.fetchrow(
                    "SELECT * FROM block WHERE height = $1", height)
                if block is None:
                    return None
                return await self._get_full_block(block, conn)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_hash_by_height(self, height: u32):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                block = await conn.fetchrow(
                    "SELECT * FROM block WHERE height = $1", height)
                if block is None:
                    return None
                return BlockHash.loads(block['block_hash'])
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_header_by_height(self, height: u32):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                block = await conn.fetchrow(
                    "SELECT * FROM block WHERE height = $1", height)
                if block is None:
                    return None
                return self._get_block_header(block)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_by_hash(self, block_hash: BlockHash | str) -> Block | None:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                block = await conn.fetchrow(
                    "SELECT * FROM block WHERE block_hash = $1", str(block_hash))
                if block is None:
                    return None
                return await self._get_full_block(block, conn)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_header_by_hash(self, block_hash: BlockHash) -> BlockHeader | None:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                block = await conn.fetchrow(
                    "SELECT * FROM block WHERE block_hash = $1", str(block_hash))
                if block is None:
                    return None
                return self._get_block_header(block)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_recent_blocks_fast(self):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                latest_height = await self.get_latest_height()
                return await Database._get_fast_block_range(latest_height, latest_height - 30, conn)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_validator_from_block_hash(self, block_hash: BlockHash) -> Address | None:
        raise NotImplementedError
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetchval(
                    "SELECT owner "
                    "FROM explorer.record r "
                    "JOIN explorer.transition ts ON r.output_transition_id = ts.id "
                    "JOIN explorer.transaction tx ON ts.transaction_id = tx.id "
                    "JOIN explorer.block b ON tx.block_id = b.id "
                    "WHERE ts.value_balance < 0 AND r.value > 0 AND b.block_hash = $1",
                    str(block_hash)
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_from_transaction_id(self, transaction_id: TransactionID | str) -> Block | None:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                block = await conn.fetchrow(
                    "SELECT b.* FROM block b JOIN transaction t ON b.id = t.block_id WHERE t.transaction_id = $1",
                    str(transaction_id)
                )
                if block is None:
                    block = await conn.fetchrow(
                        "SELECT b.* FROM block b JOIN transaction t ON b.id = t.block_id WHERE t.transaction_id = $1 ORDER BY b.height DESC LIMIT 1",
                        str(transaction_id)
                    )
                if block is None:
                    return None
                return await self._get_full_block(block, conn)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_from_transition_id(self, transition_id: TransitionID | str) -> Block | None:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                transaction_id = await conn.fetchval(
                    "SELECT tx.transaction_id FROM transaction tx "
                    "JOIN transaction_execute te ON tx.id = te.transaction_id "
                    "JOIN transition ts ON te.id = ts.transaction_execute_id "
                    "WHERE ts.transition_id = $1",
                    str(transition_id)
                ) or await conn.fetchval(
                    "SELECT tx.transaction_id FROM transaction tx "
                    "JOIN fee ON tx.id = fee.transaction_id "
                    "JOIN transition ts ON fee.id = ts.fee_id "
                    "WHERE ts.transition_id = $1",
                    str(transition_id)
                )
                if transaction_id is None:
                    return None
                return await self.get_block_from_transaction_id(transaction_id)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def search_block_hash(self, block_hash: str) -> [str]:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                result = await conn.fetch(
                    "SELECT block_hash FROM block WHERE block_hash LIKE $1", f"{block_hash}%"
                )
                if result is None:
                    return []
                return list(map(lambda x: x['block_hash'], result))
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def search_transaction_id(self, transaction_id: str) -> [str]:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                result = await conn.fetch(
                    "SELECT transaction_id FROM transaction WHERE transaction_id LIKE $1", f"{transaction_id}%"
                )
                if result is None:
                    return []
                return list(map(lambda x: x['transaction_id'], result))
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def search_transition_id(self, transition_id: str) -> [str]:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                result = await conn.fetch(
                    "SELECT transition_id FROM transition WHERE transition_id LIKE $1", f"{transition_id}%"
                )
                if result is None:
                    return []
                return list(map(lambda x: x['transition_id'], result))
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_blocks_range(self, start, end):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await Database._get_full_block_range(start, end, conn)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_blocks_range_fast(self, start, end):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await Database._get_fast_block_range(start, end, conn)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_coinbase_reward_by_height(self, height: int) -> int | None:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetchval(
                    "SELECT coinbase_reward FROM block WHERE height = $1", height
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_block_target_sum_by_height(self, height: int) -> int | None:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetchval(
                    "SELECT target_sum FROM coinbase_solution "
                    "JOIN block b on coinbase_solution.block_id = b.id "
                    "WHERE height = $1 ",
                    height
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_leaderboard_size(self) -> int:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetchval(
                    "SELECT COUNT(*) FROM leaderboard"
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_leaderboard(self, start: int, end: int) -> list:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetch(
                    "SELECT * FROM leaderboard "
                    "ORDER BY total_incentive DESC, total_reward DESC "
                    "LIMIT $1 OFFSET $2",
                    end - start, start
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_leaderboard_rewards_by_address(self, address: str) -> (int, int):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "SELECT total_reward, total_incentive FROM leaderboard WHERE address = $1", address
                )
                return row['total_reward'], row['total_incentive']
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_recent_solutions_by_address(self, address: str) -> list:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetch(
                    "SELECT b.height, b.timestamp, ps.nonce, ps.target, reward, cs.target_sum "
                    "FROM partial_solution ps "
                    "JOIN coinbase_solution cs ON cs.id = ps.coinbase_solution_id "
                    "JOIN block b ON b.id = cs.block_id "
                    "WHERE ps.address = $1 "
                    "ORDER BY cs.id DESC "
                    "LIMIT 30",
                    address
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_solution_count_by_address(self, address: str) -> int:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetchval(
                    "SELECT COUNT(*) FROM partial_solution WHERE address = $1", address
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_solution_by_address(self, address: str, start: int, end: int) -> list:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetch(
                    "SELECT b.height, b.timestamp, ps.nonce, ps.target, reward, cs.target_sum "
                    "FROM partial_solution ps "
                    "JOIN coinbase_solution cs ON cs.id = ps.coinbase_solution_id "
                    "JOIN block b ON b.id = cs.block_id "
                    "WHERE ps.address = $1 "
                    "ORDER BY cs.id DESC "
                    "LIMIT $2 OFFSET $3",
                    address, end - start, start
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_solution_by_height(self, height: int, start: int, end: int) -> list:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetch(
                    "SELECT ps.address, ps.nonce, ps.commitment, ps.target, reward "
                    "FROM partial_solution ps "
                    "JOIN coinbase_solution cs on ps.coinbase_solution_id = cs.id "
                    "JOIN block b on cs.block_id = b.id "
                    "WHERE b.height = $1 "
                    "ORDER BY target DESC "
                    "LIMIT $2 OFFSET $3",
                    height, end - start, start
                )
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def search_address(self, address: str) -> [str]:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                result = await conn.fetch(
                    "SELECT address FROM leaderboard WHERE address LIKE $1", f"{address}%"
                )
                if result is None:
                    return []
                return list(map(lambda x: x['address'], result))
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_address_speed(self, address: str) -> (float, int): # (speed, interval)
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            interval_list = [900, 1800, 3600, 14400, 43200, 86400]
            now = int(time.time())
            try:
                for interval in interval_list:
                    partial_solutions = await conn.fetch(
                        "SELECT b.height FROM partial_solution ps "
                        "JOIN coinbase_solution cs ON ps.coinbase_solution_id = cs.id "
                        "JOIN block b ON cs.block_id = b.id "
                        "WHERE address = $1 AND timestamp > $2",
                        address, now - interval
                    )
                    if partial_solutions is None or len(partial_solutions) < 10:
                        continue
                    heights = list(map(lambda x: x['height'], partial_solutions))
                    ref_heights = list(map(lambda x: x - 1, set(heights)))
                    ref_proof_targets = await conn.fetch(
                        "SELECT height, proof_target FROM block WHERE height = ANY($1::bigint[])", ref_heights
                    )
                    ref_proof_target_dict = dict(map(lambda x: (x['height'], x['proof_target']), ref_proof_targets))
                    total_solutions = 0
                    for height in heights:
                        total_solutions += ref_proof_target_dict[height - 1]
                    return total_solutions / interval, interval
                return 0, 0
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_network_speed(self) -> float:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            now = int(time.time())
            interval = 900
            try:
                partial_solutions = await conn.fetch(
                    "SELECT b.height FROM partial_solution ps "
                    "JOIN coinbase_solution cs ON ps.coinbase_solution_id = cs.id "
                    "JOIN block b ON cs.block_id = b.id "
                    "WHERE timestamp > $1",
                    now - interval
                )
                heights = list(map(lambda x: x['height'], partial_solutions))
                ref_heights = list(map(lambda x: x - 1, set(heights)))
                ref_proof_targets = await conn.fetch(
                    "SELECT height, proof_target FROM block WHERE height = ANY($1::bigint[])", ref_heights
                )
                ref_proof_target_dict = dict(map(lambda x: (x['height'], x['proof_target']), ref_proof_targets))
                total_solutions = 0
                for height in heights:
                    total_solutions += ref_proof_target_dict[height - 1]
                return total_solutions / interval
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_leaderboard_total(self) -> int:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                total_credit = await conn.fetchval("SELECT total_credit FROM leaderboard_total")
                if total_credit is None:
                    await conn.execute("INSERT INTO leaderboard_total (total_credit) VALUES (0)")
                    total_credit = 0
                return int(total_credit)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def get_puzzle_commitment(self, commitment: str) -> dict | None:
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "SELECT reward, height FROM partial_solution "
                    "JOIN coinbase_solution cs on cs.id = partial_solution.coinbase_solution_id "
                    "JOIN block b on b.id = cs.block_id "
                    "WHERE commitment = $1",
                    commitment
                )
                if row is None:
                    return None
                return {
                    'reward': row['reward'],
                    'height': row['height']
                }
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise


    # migration method
    async def update_target_sum(self):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                latest_height = await self.get_latest_height()
                for height in range(latest_height):
                    target_sum = await conn.fetchval("SELECT get_block_target_sum($1)", height)
                    if target_sum is not None:
                        print(f"Updating target sum for height {height}")
                        coinbase_solution_id = await conn.fetchval(
                            "SELECT cs.id FROM coinbase_solution cs "
                            "JOIN block b ON b.id = cs.block_id "
                            "WHERE b.height = $1", height
                        )
                        await conn.execute("UPDATE coinbase_solution SET target_sum = $1 WHERE id = $2", target_sum, coinbase_solution_id)
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    # debug method
    async def clear_database(self):
        conn: asyncpg.Connection
        async with self.pool.acquire() as conn:
            try:
                await conn.execute("TRUNCATE TABLE block CASCADE")
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise