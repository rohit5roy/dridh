import express from 'express'
import { normalise } from './orders/service'

const app = express()

app.get('/health', (req, res) => res.send(normalise('ok')))

export default app
